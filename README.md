# OhMySmell Backend / CRM

FastAPI + async SQLAlchemy/PostgreSQL, Telegram manager review, email ingestion,
server-authoritative pricing and local warehouse fulfillment plans.

- [Architecture and flows](docs/ARCHITECTURE.md)
- [Tilda API contract](docs/TILDA.md)
- [OpenAPI snapshot](docs/openapi.json)
- [Environments, migration, launch and rollback](docs/OPERATIONS.md)
- [Audit findings](docs/STAGING_AUDIT.md)
- [Finish report](docs/FINISH_REPORT.md)

Use the project virtual environment:

```powershell
.\.venv\Scripts\python.exe -m scripts.install_dependencies
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q app tests alembic scripts
```

Copy .env.example to .env and configure an explicitly selected local/staging DB.
Never commit credentials or use production DB for experiments.

```powershell
.\.venv\Scripts\python.exe -m scripts.staging_probe
.\.venv\Scripts\python.exe -m scripts.validate_staging
.\.venv\Scripts\python.exe -m scripts.run_staging_tests
.\.venv\Scripts\python.exe -m app.api.staging_runner
```

The local staging API listens at http://127.0.0.1:8001; /docs describes schemas.
For the bot use the project interpreter with -m app.bot.staging_runner.
Use one poller per token. Workers and deployment are described in OPERATIONS.md.

EXTERNAL_WRITES_ENABLED=false and PUBLIC_CHECKOUT_ENABLED=false are safe defaults.
The staging API runner enables only staging checkout. Finalize creates only a
local order; warehouse allocations are plans, not external reservations.
The confirmed wholesale price type is Цена продажи. Retail has no fallback and
remains in manager review until its own price is configured.

Real Gmail OAuth, production migrations/deployment, Tilda connection, delivery
orders and MoySklad writes require their separate activation steps.


Release candidate operations: [Manager bot](docs/MANAGER_BOT.md),
[Client bot](docs/CLIENT_BOT.md), [Order/payment lifecycle](docs/ORDER_LIFECYCLE.md).
The standalone worker command is `python -m app.bot.runtime`; set BOT_ROLE=manager
or client (different tokens). The API never polls Telegram. PostgreSQL advisory
ownership prevents two managed pollers; /health reports role/readiness without secrets.
Manager workflow supports local fulfillment, payment recording and delivery tracking.
External writes remain disabled. Client intake always goes through manager review.
Internal POST /orders now requires Idempotency-Key; actions require a manager actor
and expected revision. See the updated OpenAPI snapshot.

## Internal readiness continuation

Independent launchers: `python -m app.workers.manager_bot`,
`python -m app.workers.client_bot`, `python -m app.workers.email_runtime`.
Backend remains Uvicorn only. `/health` is liveness, `/ready` is readiness.
Workers do not require unrelated backend CORS/internal tokens.

Operations: [Production configuration](docs/PRODUCTION_READINESS.md),
[External connection checklists](docs/EXTERNAL_INTEGRATIONS.md),
[Backup/restore rehearsal](docs/BACKUP_RESTORE.md),
[Incident response](docs/INCIDENT_RUNBOOK.md), [PII and test fixtures](docs/DATA_RETENTION.md).
Production template: `deploy/production.env.example`; prepared future services:
`deploy/worker-services.json`. Optional monitoring is disabled by default.
