"""Create/preview/drop only tool-owned isolated synthetic schemas on staging."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4
from sqlalchemy import create_engine, text, inspect
from alembic import command
from alembic.config import Config


def validate_manifest(manifest, run, database_fingerprint):
    if not re.fullmatch(r"[a-f0-9]{32}", run):
        raise ValueError("Invalid synthetic run ID")
    if manifest != {"run": run, "schema": "oms_fixture_" + run, "database": database_fingerprint}:
        raise ValueError("Manifest does not belong to this staging database/run")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["create", "preview", "cleanup"])
    parser.add_argument("--run")
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv()
    from app.bot.staging_runner import validate_staging_config, activate_staging_config
    config = validate_staging_config(dict(os.environ)); activate_staging_config(config)
    run = uuid4().hex if args.action == "create" else args.run or ""
    if not re.fullmatch(r"[a-f0-9]{32}", run):
        raise ValueError("An exact synthetic run ID is required")
    db_hash = hashlib.sha256(config.database_url.encode()).hexdigest()
    path = Path(".staging-artifacts") / ("synthetic_" + run + ".json")
    manifest = {"run": run, "schema": "oms_fixture_" + run, "database": db_hash}
    if args.action != "create":
        manifest = json.loads(path.read_text())
    validate_manifest(manifest, run, db_hash)
    schema = manifest["schema"]
    engine = create_engine(config.database_url, connect_args={"connect_timeout": 10, "options": "-c statement_timeout=30000"})
    try:
        with engine.connect() as conn:
            if args.action == "create":
                conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                conn.execute(text(f'SET search_path TO "{schema}"'))
                conn.commit(); conn.dialect.default_schema_name = schema
                cfg = Config("alembic.ini"); cfg.attributes["connection"] = conn
                command.upgrade(cfg, "head"); conn.commit()
                conn.execute(text("CREATE TABLE synthetic_run_marker (run text PRIMARY KEY)"))
                conn.execute(text("INSERT INTO synthetic_run_marker VALUES (:run)"), {"run": run})
                conn.execute(text("INSERT INTO customers (customer_type,display_name) VALUES ('unknown',:name)"), {"name": "SYNTHETIC " + run})
                conn.commit()
                path.write_text(json.dumps(manifest), encoding="utf-8")
            else:
                marker = conn.scalar(text(f'SELECT run FROM "{schema}".synthetic_run_marker'))
                if marker != run:
                    raise ValueError("Ownership marker mismatch; cleanup refused")
                tables = inspect(conn).get_table_names(schema=schema)
                print(json.dumps({"schema": schema, "tables": len(tables), "action": args.action}))
                if args.action == "cleanup":
                    # Exact freshly-created schema, manifest and in-DB marker all
                    # agree. Never accepts public or arbitrary existing schemas.
                    conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                    conn.commit()
            print(json.dumps({"run": run, "schema": schema, "action": args.action, "passed": True}))
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"passed": False, "error_type": type(error).__name__}))
        raise SystemExit(1)
