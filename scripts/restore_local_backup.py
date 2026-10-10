"""Restore an existing snapshot only into a new database of a verified local cluster."""
import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from sqlalchemy.engine import make_url
from sqlalchemy import text
from scripts.backup_restore_rehearsal import pg_run, engine_for, fingerprint


def verify_local(connection, directory):
    with connection.cursor() as cursor:
        cursor.execute('SHOW data_directory')
        if Path(cursor.fetchone()[0]).resolve() != Path(directory).resolve():
            raise ValueError('Unexpected local cluster directory')
        # inet::text includes /32; host() normalizes without weakening the check.
        cursor.execute('SELECT host(inet_server_addr()), inet_server_port()')
        if cursor.fetchone() != ('127.0.0.1', 55439):
            raise ValueError('Unexpected local endpoint')


def restore(root, directory):
    root = Path(root).resolve()
    archive = root / 'staging-before-q60.dump'
    before = json.loads((root / 'backup-report.json').read_text(encoding='utf-8'))
    if hashlib.sha256(archive.read_bytes()).hexdigest() != before['sha256']:
        raise ValueError('Backup checksum mismatch')
    name = 'oms_restore_' + uuid4().hex
    connection = psycopg2.connect(host='127.0.0.1', port=55439, user='oms_test',
                                  dbname='postgres', connect_timeout=10)
    try:
        connection.autocommit = True
        verify_local(connection, directory)
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1 FROM pg_database WHERE datname=%s', (name,))
            if cursor.fetchone() is not None:
                raise ValueError('Restore target already exists')
            cursor.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
    finally:
        connection.close()
    url = make_url('postgresql://oms_test@127.0.0.1:55439/' + name)
    # PostgreSQL template0 already contains public, while this archive creates
    # public itself. Remove only the empty schema of the DB just created above.
    with psycopg2.connect(host='127.0.0.1', port=55439, user='oms_test', dbname=name) as fresh:
        verify_local(fresh, directory)
        with fresh.cursor() as cursor:
            cursor.execute('SELECT current_database()')
            if cursor.fetchone() != (name,):
                raise ValueError('Unexpected restore database')
            cursor.execute("SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace")
            if cursor.fetchone() != (0,):
                raise ValueError('Restore schema is not empty')
            cursor.execute('DROP SCHEMA public')
    pg_run('pg_restore', ['--no-owner', '--no-acl', '--exit-on-error', '--single-transaction',
                         '--dbname=' + name, str(archive)], url)
    engine = engine_for(url)
    try:
        with engine.connect() as connection:
            actual = json.loads(json.dumps(fingerprint(connection)))
            expected = json.loads((root / 'source-fingerprint.json').read_text(encoding='utf-8'))
            if actual != expected:
                # Compatibility with the existing en_US.utf8 snapshot: change
                # ordering only, never row contents. Full old digest must match.
                legacy = json.loads(json.dumps(actual))
                for table in legacy:
                    quoted = connection.dialect.identifier_preparer.quote(table)
                    rows = connection.execute(text(f'SELECT row_to_json(t)::text FROM public.{quoted} t')).scalars().all()
                    rows.sort(key=lambda row: (''.join(c for c in row if c.isalnum()).casefold(), row))
                    legacy[table]['sha256'] = hashlib.sha256('\n'.join(rows).encode()).hexdigest()
                if legacy == expected:
                    actual = legacy
    finally:
        engine.dispose()
    expected = json.loads((root / 'source-fingerprint.json').read_text(encoding='utf-8'))
    if actual != expected:
        raise ValueError('Restored fingerprint mismatch')
    protected = {}
    with psycopg2.connect(host='127.0.0.1', port=55439, user='oms_test', dbname=name,
                         options='-c default_transaction_read_only=on -c timezone=UTC') as connection:
        with connection.cursor() as cursor:
            for table, where in (('draft_orders','id=38'), ('draft_order_items','draft_order_id=38'),
                                 ('orders','id=16'), ('order_items','order_id=16')):
                cursor.execute(f'SELECT row_to_json(t)::text FROM {table} t WHERE {where} ORDER BY id')
                rows = cursor.fetchall()
                if not rows:
                    raise ValueError('Protected rows missing')
                protected[table] = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
            cursor.execute('SELECT order_id FROM order_desks WHERE draft_id=38')
            if cursor.fetchone() != (16,):
                raise ValueError('Protected order linkage mismatch')
    preflight = json.loads((root / 'preflight-before.json').read_text(encoding='utf-8'))
    if protected != preflight['protected_hashes']:
        raise ValueError('Protected snapshot mismatch')
    report = dict(restore='passed', database=name, fingerprint='exact_match',
                  tables=len(actual), rows=sum(t['rows'] for t in actual.values()),
                  protected_hashes=protected, draft38_order16_link_valid=True,
                  archive_sha256=before['sha256'], migration_executed=False)
    target = root / (name + '-report.json')
    target.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return {k:v for k,v in report.items() if k != 'protected_hashes'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup_directory')
    parser.add_argument('--cluster', default='.staging-artifacts/acceptance-pg-20261010')
    args = parser.parse_args()
    try:
        print(json.dumps(restore(args.backup_directory, args.cluster)))
    except Exception as error:
        print(json.dumps({'restore': 'STOP', 'error_type': type(error).__name__}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
