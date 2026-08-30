# OhMySmell Backend

FastAPI backend for sales orders, customer identities, email draft ingestion,
Telegram manager review, and explicitly triggered MoySklad export.

## Local development

Copy `.env.example` to `.env` and provide local or staging credentials. Never
commit `.env`, Gmail OAuth credentials, Gmail tokens, or service tokens.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q app tests alembic scripts
```

Tests use fake providers and do not write to Gmail, Telegram, MoySklad, or
production PostgreSQL.

## FastAPI

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Manager/internal endpoints require `X-Internal-API-Token`. Debug endpoints
also require `DEBUG_ENDPOINTS_ENABLED=true`; they return 404 by default.

External business-data writes are disabled by default with
`EXTERNAL_WRITES_ENABLED=false`. Read-only MoySklad catalog, stock,
counterparty, organization, and health operations remain available. Enable
external writes only through an explicit environment setting after staging
verification.

## Email worker

Create a Google OAuth Desktop application with only the Gmail read-only scope.
Store its JSON outside the repository and configure `GMAIL_CREDENTIALS_FILE`
and `GMAIL_TOKEN_FILE`.

```powershell
.\.venv\Scripts\python.exe -m app.workers.email_ingestion
```

The first OAuth run opens user consent. The worker never sends mail, modifies
labels, marks messages read, deletes, or archives. It persists Gmail
`historyId` in PostgreSQL and retains `external_message_id` uniqueness as the
last idempotency boundary.

Without Gmail access, run one explicit staging-only message through the same
ingestion pipeline:

```powershell
.\.venv\Scripts\python.exe -m app.workers.staging_fake_email
```

The command requires a distinct `STAGING_DATABASE_URL` and
`EXTERNAL_WRITES_ENABLED=false`. It is never started automatically. It uses
the existing fake provider, read-only MoySklad matching/pricing, local draft
storage, and the normal Telegram manager notification.

## Telegram bot

```powershell
.\.venv\Scripts\python.exe run_bot.py
```

Telegram users must be active rows in `managers`. Review callbacks call the
same draft services used by the internal API. Finalize creates only a local
order and never creates a MoySklad customer order automatically.

## Migrations: fresh database

Create a new empty PostgreSQL database, point `DATABASE_URL` to it, and run:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

The additive `f01a2b3c4d5e` reconciliation baseline creates historical
`orders`/`order_items`; the normal chain builds the complete schema. Money
columns contain integer minor units (kopecks) at head.

## Existing production database procedure

Do not run this procedure without a backup and reviewed schema dump.

1. Stop application writes and make a verified backup.
2. Inspect `alembic_version`; compare actual objects to models and migrations.
3. If the legacy DB has `managers`, `orders`, and `order_items` but no version,
   stamp only `d678cecfa969` after verifying the managers schema.
4. Review migration SQL and the reconciliation checks.
5. Pay special attention to `d38f9a21bc54`: it assumes historical monetary
   integers are rubles and multiplies them by 100 while converting to BIGINT.
6. Restore a production backup into disposable staging PostgreSQL and run
   `alembic upgrade head` there first.
7. Validate row counts, totals, foreign keys, constraints, and smoke tests.
8. Only then schedule the production upgrade.

Never blindly stamp `head`: stamping does not create missing customer, draft,
cursor, or constraint objects.

## Required environment variables

See `.env.example`. Staging requires PostgreSQL, MoySklad read access,
Telegram, Gmail OAuth paths, a strong internal API token, and a polling
interval. `MOYSKLAD_WHOLESALE_PRICE_TYPE=Цена продажи` is the confirmed
wholesale mapping. Retail pricing remains unconfigured and must never fall
back to wholesale.
