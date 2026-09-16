# Backup / restore and migration rehearsal

## Executed staging rehearsal, 2026-09-16

`python -m scripts.backup_restore_rehearsal` uses the explicit
STAGING_DATABASE_URL, rejects equality with DATABASE_URL, and enforces disabled
external writes. It never runs a restore against that source URL.

Native PostgreSQL 18 pg_dump/pg_restore: exported REPEATABLE READ snapshot,
custom archive of the application public schema, no ownership/ACL restoration.
19 tables / 259 rows restored; table columns/nullability, sorted row SHA-256,
sequences and Alembic head verified. Restored schema passes alembic check.
Backup SHA-256: e2a07ff3d621f8e7695c09ac3aaa3fd391789a80554f98177df17ba6b4ce47df.
Four newly-created oms_rehearsal_<random UUID> databases were removed afterwards.
No staging rows were modified by this procedure.

Also executed: empty database -> head; historical i82c4f76ab09 database with a
synthetic customer -> dump -> another new DB -> upgrade j93d5087bc10, customer
unchanged. Destructive downgrade j93 -> i82 is deliberately refused and was
verified only on the disposable restored DB. No blind stamp.

The first restore attempt failed because the new DB already contained public;
`--clean --if-exists` is used ONLY on the fresh generated target. Source guards
and exact-name ownership checks precede every disposable DROP DATABASE.

## Reproduce

Install pg_dump and pg_restore >= the server major version. PATH or PG_BIN points
to their directory. Run `python -m scripts.backup_restore_rehearsal`.
Artifacts: `.staging-artifacts/backups/*.dump`, `backup_restore.json` (ignored).
Archives contain customer PII; use restricted filesystem access and encrypted
storage, do not attach to issues/chat or commit. Retain rehearsal archives 7 days,
then delete exact approved filenames. The JSON receipt has no row contents.

## Future production procedure — not executed

1. Authorize maintenance, freeze application writes, record deployed commit and
   `alembic current`. Take provider snapshot plus custom pg_dump, preserve roles/
   grants separately. Secrets reside in a restricted pgpass/service file, never
   command arguments. Use TLS verify-full on public connections.
2. With PGHOST/PGPORT/PGUSER/PGDATABASE and PGPASSFILE set securely:
   `pg_dump --format=custom --no-owner --no-acl --file=backup.dump`.
   This full-database command intentionally omits the rehearsal public filter.
3. Create a NEW restore database on a disposable server. Set PGDATABASE to that
   new target after independently verifying it differs from production. Run
   `pg_restore --exit-on-error --no-owner --no-acl --dbname=NEW_DATABASE backup.dump`.
   Do not use --clean against an existing live target.
4. Point an isolated application process at the restored DB. Check alembic current,
   row counts/checksums, FKs/constraints, sequences, draft/order/payment linkage.
   Run `alembic upgrade head`, `alembic check`, and synthetic flows with writes off.
5. Define RPO <=24 h initially (nightly encrypted backup), RTO target <=1 h; these
   are operational targets, not measured production guarantees. Rehearse monthly
   and before migration. Keep 7 daily +4 weekly backups; verify restore, not just dump.

Rollback of additive releases: redeploy compatible previous application while
retaining added columns/tables. Do not downgrade/delete audit. If data repair is
required, use a reviewed forward migration. Disaster restoration uses a new DB
and a reviewed connection switch; account for transactions after backup time.
