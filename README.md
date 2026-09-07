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
