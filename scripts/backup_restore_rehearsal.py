"""Staging-only pg_dump/pg_restore rehearsal; never restores into the source DB.
Creates three disposable databases on the staging server, validates and drops only
those exact newly-created names. Dumps stay in ignored .staging-artifacts/backups.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from uuid import uuid4
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from alembic import command
from alembic.config import Config


def binary(name):
    executable = shutil.which(name)
    if not executable:
        candidate = Path(os.getenv("PG_BIN", "C:/Program Files/PostgreSQL/18/bin")) / (name + (".exe" if os.name == "nt" else ""))
        if candidate.is_file():
            executable = str(candidate)
    if not executable:
        raise RuntimeError("PostgreSQL client tools required")
    return executable


def pg_environment(url):
    return dict(os.environ, PGHOST=url.host, PGPORT=str(url.port or 5432),
        PGUSER=url.username, PGPASSWORD=url.password or "", PGDATABASE=url.database,
        PGCONNECT_TIMEOUT="10", PGSSLMODE=url.query.get("sslmode", "prefer"))


def pg_run(name, args, url):
    result = subprocess.run([binary(name), *args], env=pg_environment(url),
        capture_output=True, timeout=240)
    if result.returncode:
        # pg stderr may contain row contents, hostnames and user names.
        Path(".staging-artifacts/pg_failure.txt").write_bytes(result.stderr)
        raise RuntimeError(name + " failed; exit=" + str(result.returncode))


def fingerprint(conn):
    result = {}
    inspector = inspect(conn)
    for table in sorted(inspector.get_table_names(schema="public")):
        quoted = conn.dialect.identifier_preparer.quote(table)
        rows = conn.execute(text(f'SELECT row_to_json(t)::text FROM public.{quoted} t ORDER BY row_to_json(t)::text')).scalars().all()
        result[table] = {"rows": len(rows), "sha256": hashlib.sha256("\n".join(rows).encode()).hexdigest(),
            "columns": [(c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(table, schema="public")]}
    return result


def engine_for(url):
    return create_engine(url, connect_args={"connect_timeout": 10, "options": "-c statement_timeout=60000"})


def migrate(url, revision="head", check=False):
    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            config = Config("alembic.ini")
            config.attributes["connection"] = conn
            command.upgrade(config, revision)
            conn.commit()
            if check:
                command.check(config)
            return conn.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        engine.dispose()


def backup(url, path):
    engine = engine_for(url)
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            snapshot = conn.scalar(text("SELECT pg_export_snapshot()"))
            before = fingerprint(conn)
            pg_run("pg_dump", ["--format=custom", "--no-owner", "--no-acl", "--schema=public",
                "--snapshot=" + snapshot, "--file=" + str(path)], url)
            return before
    finally:
        engine.dispose()


def main():
    from dotenv import load_dotenv
    load_dotenv()
    from app.bot.staging_runner import validate_staging_config, activate_staging_config
    source = validate_staging_config(dict(os.environ))
    activate_staging_config(source)
    url = make_url(source.database_url)
    directory = Path(".staging-artifacts/backups")
    directory.mkdir(parents=True, exist_ok=True)
    run = uuid4().hex
    report = {"run": run, "environment": "staging", "source_modified": False, "checks": []}
    admin = engine_for(url)
    created = []
    def disposable():
        name = "oms_rehearsal_" + uuid4().hex
        assert re.fullmatch(r"oms_rehearsal_[a-f0-9]{32}", name) and name != url.database
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        created.append(name)
        return url.set(database=name)
    try:
        dump = directory / (run + "-staging.dump")
        before = backup(url, dump)
        restored = disposable()
        pg_run("pg_restore", ["--exit-on-error", "--clean", "--if-exists", "--no-owner", "--no-acl", "--dbname=" + restored.database, str(dump)], restored)
        engine = engine_for(restored)
        try:
            with engine.connect() as conn:
                assert fingerprint(conn) == before, "Restored schema/data differ"
                # Sequences must safely allocate IDs above the restored rows.
                for table in inspect(conn).get_table_names(schema="public"):
                    columns = {c["name"] for c in inspect(conn).get_columns(table)}
                    if "id" not in columns:
                        continue
                    seq = conn.scalar(text("SELECT pg_get_serial_sequence(:table, 'id')"), {"table": table})
                    if seq:
                        maximum = conn.scalar(text(f'SELECT max(id) FROM "{table}"')) or 0
                        assert conn.scalar(text("SELECT nextval(CAST(:seq AS regclass))"), {"seq": seq}) > maximum
                report["restored_head"] = conn.scalar(text("SELECT version_num FROM alembic_version"))
        finally:
            engine.dispose()
        migrate(restored, check=True)
        report["checks"].append("staging_snapshot_restore_schema_rows_hashes_sequences_head")
        report["tables"] = len(before)
        report["rows"] = sum(t["rows"] for t in before.values())
        report["backup_sha256"] = hashlib.sha256(dump.read_bytes()).hexdigest()
        fresh = disposable()
        report["fresh_head"] = migrate(fresh, check=True)
        report["checks"].append("empty_database_upgrade_check")
        historical = disposable()
        migrate(historical, "i82c4f76ab09")
        engine = engine_for(historical)
        try:
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO customers (customer_type,display_name) VALUES ('unknown','SYNTHETIC migration rehearsal')"))
        finally:
            engine.dispose()
        historical_dump = directory / (run + "-historical.dump")
        historical_before = backup(historical, historical_dump)
        historical_restored = disposable()
        pg_run("pg_restore", ["--exit-on-error", "--clean", "--if-exists", "--no-owner", "--no-acl", "--dbname=" + historical_restored.database, str(historical_dump)], historical_restored)
        report["historical_head"] = migrate(historical_restored, check=True)
        engine = engine_for(historical_restored)
        try:
            with engine.connect() as conn:
                after = fingerprint(conn)
                assert after["customers"] == historical_before["customers"]
                report["checks"].append("historical_restore_upgrade_data_preserved")
                config = Config("alembic.ini")
                config.attributes["connection"] = conn
                try:
                    command.downgrade(config, "i82c4f76ab09")
                except RuntimeError:
                    conn.rollback()
                    report["checks"].append("destructive_downgrade_refused")
                else:
                    raise AssertionError("Audit-preserving downgrade refusal expected")
        finally:
            engine.dispose()
        report["passed"] = True
    finally:
        dropped = 0
        for name in created:
            assert re.fullmatch(r"oms_rehearsal_[a-f0-9]{32}", name) and name != url.database
            with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(f'DROP DATABASE "{name}"'))
            dropped += 1
        admin.dispose()
        report["disposable_databases_removed"] = dropped
        Path(".staging-artifacts/backup_restore.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"passed": False, "error_type": type(error).__name__}))
        raise SystemExit(1)
