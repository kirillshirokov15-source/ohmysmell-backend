# Staging, production activation и rollback

## Запуск

Использовать проектный `.venv\Scripts\python.exe`. Venv не хранится в Git.
requirements — UTF-8 с фиксированными версиями. Windows install helper использует
доверенные Windows certificates + certifi, без отключения проверки TLS.

```powershell
.\.venv\Scripts\python.exe -m scripts.install_dependencies
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q app tests alembic scripts
.\.venv\Scripts\python.exe -m scripts.staging_probe
.\.venv\Scripts\python.exe -m scripts.validate_staging
.\.venv\Scripts\python.exe -m scripts.run_staging_tests
.\.venv\Scripts\python.exe -m scripts.staging_upgrade
.\.venv\Scripts\python.exe -m app.api.staging_runner
```

Development использует отдельную локальную DB. Staging scripts сравнивают
STAGING_DATABASE_URL и DATABASE_URL до импорта DB engine, production connection
не открывают. API runner слушает `127.0.0.1:8001`, включает только staging checkout,
выключает внешние writes. Если STAGING_INTERNAL_API_TOKEN отсутствует, создаёт
сильный token в игнорируемом `.staging-artifacts/internal-api-token.txt`.
Читать его только локально, не передавать в браузер/логи/Git.

В отдельном Railway staging deployment DATABASE_URL должен указывать именно на
staging DB, APP_ENV=staging, EXTERNAL_WRITES_ENABLED=false. Telegram token нужен
только при отдельном включении уведомлений/bot; API без него сохраняет заявки и
notification intents. Для bot требуется отдельный staging token.
Локальные scripts используют исходный DATABASE_URL только для
проверки различия сред. Не подменять им production для экспериментов.

| Процесс | Команда после явной настройки среды |
|---|---|
| API | `python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Bot | `python run_bot.py` / локально `python -m app.bot.staging_runner` |
| Уведомления | `python -m app.workers.notifications` |
| Email | `python -m app.workers.email_ingestion` после OAuth |

Один poller на Telegram token. Staging bot runner очищает pending updates; не
использовать общий production token при параллельном production poller.
Dockerfile запускает API непривилегированным пользователем без secrets и auto
migrations. Проверенный remote staging использует этот Dockerfile; результаты
приёмки приведены в `docs/REMOTE_STAGING_ACCEPTANCE.md`. Workers запускаются отдельно.

API startup включает INFO-логи только для `app`, отправляя их в stdout.
`api_request_completed` содержит route template и duration_ms; MoySklad gateway
пишет duration/cache hit/miss, `/health/db` — `database_health_completed` с
duration_ms, connected и только классом ошибки. SQL echo и подробные HTTP-логи
зависимостей не включаются; credentials и текст ошибок подключения не логируются.

## ENV reference

| Переменная | Назначение / безопасный default |
|---|---|
| APP_NAME | OhMySmell CRM |
| APP_ENV | development / staging / production; development |
| DATABASE_URL | DB выбранной среды процесса; не печатать |
| STAGING_DATABASE_URL | Только явные локальные staging runners; без fallback |
| INTERNAL_API_TOKEN | Internal HTTP API; отсутствие закрывает доступ |
| STAGING_INTERNAL_API_TOKEN | Отдельный token локального staging API |
| DEBUG_ENDPOINTS_ENABLED | false; production запрещает true |
| PUBLIC_CHECKOUT_ENABLED | false; отдельное включение после проверки сайта |
| CORS_ORIGINS | Точные origins через запятую; development localhost |
| EXTERNAL_WRITES_ENABLED | false; staging runtime запрещает true |
| MOYSKLAD_TOKEN | Existing API token; не выводить |
| MOYSKLAD_WHOLESALE_PRICE_TYPE | Цена продажи |
| MOYSKLAD_RETAIL_PRICE_TYPE | Пусто; retail требует review |
| MOYSKLAD_ORGANIZATION_ID | Пусто; обязательно перед writes |
| MOYSKLAD_WAREHOUSE_IDS | Разрешённые store IDs через запятую; пусто = все active |
| MOYSKLAD_CATALOG_CACHE_TTL_SECONDS | 60; process-local catalog |
| MOYSKLAD_CA_BUNDLE | Дополнительный проверенный CA bundle, необязателен |
| REQUESTS_CA_BUNDLE | Совместимый fallback CA path |
| TELEGRAM_BOT_TOKEN | Отдельные staging/production tokens |
| GMAIL_CREDENTIALS_FILE | OAuth client JSON вне Git |
| GMAIL_TOKEN_FILE | OAuth token вне Git |
| GMAIL_USER_ID | me |
| GMAIL_INITIAL_QUERY | label:inbox is:unread |
| EMAIL_POLL_INTERVAL | 60 секунд |
| CDEK_CLIENT_ID | Пусто; будущий СДЭК adapter |
| CDEK_CLIENT_SECRET | Пусто; будущий СДЭК adapter |
| YANDEX_DELIVERY_TOKEN | Пусто; будущий Yandex adapter |
| PORT | Railway port, Docker fallback 8000 |

OMS_STAGING_TESTS/OMS_STAGING_SCHEMA задаются только test runner; PIP_CERT — install
helper. Production startup требует internal token >=32 символа и явные HTTPS
origins. Retail mapping никогда не должен совпадать с wholesale.

## Миграции

Fresh rehearsal создаёт уникальную `oms_audit_<uuid>` schema в staging. Там
проверяются migrations/models и concurrency. Артефакты `.staging-artifacts` не
коммитятся. Audit schemas сохранены; удалять можно только конкретные собственные
схемы после проверки имени. API/bot используют основную staging schema.

- g60a2d54ef87: revision, channel sources, shipments/allocations, checkout,
  delivery requests, external operation journal.
- h71b3e65fa98: notification queue, исправление orders.status default.
- i82c4f76ab09: контакты website и money CHECK constraints.

CHECK constraints добавлены NOT VALID: новые записи защищены, historical rows
нужно отдельно проверить и затем VALIDATE CONSTRAINT. Automatic destructive
downgrade новых миграций запрещён; старый money downgrade теряет копейки.

## Production migration checklist

1. Получить отдельное разрешение, зафиксировать code revision, Alembic graph,
   фактическую production схему и текущие row counts/sums/NULLs/FK.
2. Остановить бизнес-записи API/bot/email/export, сделать backup и проверить restore.
3. Восстановить historical backup в отдельную staging DB, сопоставить таблицы,
   ранее созданные scripts, с фактической alembic_version.
4. Не выполнять blind stamp. При отсутствии версии выбрать конкретный stamp только
   после полного schema comparison и review, либо написать reconciliation migration.
5. Перед d38f9a21bc54 проверить единицы денег: она предполагает исторические рубли
   и умножает integers на 100. Уже существующие копейки или смешанные единицы —
   STOP, отдельный reviewed data plan; не умножать повторно.
6. Отрепетировать upgrade head на копии, выполнить alembic check, сравнить PK/FK,
   unique/check/defaults, положительные qty, item_total=price*qty, сумму строк заказов.
7. Проверить historical money constraints; после review выполнить VALIDATE
   CONSTRAINT на staging, затем в разрешённое окно на production.
8. Только после успешной репетиции — production migration, оба флага checkout и
   external writes false. После неё проверить версию, health, auth и read-only smoke.

## Production launch checklist

- DB credential из прежнего аудита уже rotated перед remote acceptance;
  повторно не использовать старое значение. В Git секрет не попал.
- Подтвердить production URL, migration, backup/PITR и rollback.
- HTTPS, реальные Tilda CORS origins, доверенные proxy headers, ограничение частоты
  и anti-bot checkout на reverse proxy/WAF. CORS не заменяет auth.
- Сильный internal token, ограниченный runtime DB role, отдельный migration role,
  managers allowlist; отдельные staging/production bot tokens.
- Настроить две warehouse IDs и organization; подтвердить приоритет складов.
- Получить решение владельца по retail price. До этого review, без автоматической
  оплаты; «Цена продажи» остаётся wholesale.
- Подключить Tilda по docs/TILDA.md, проверить errors, retry и idempotency.
- Мониторить 5xx, DB pool, callback duration, очередь/attempts, failed email batches,
  inflight/uncertain внешние операции; проверить alerts.
- Запустить API, один poller и notification worker; Gmail только после отдельного
  OAuth с gmail.readonly. Включить public checkout после принятого smoke.
- Внешние writes включать отдельным решением с supervised canary и повторной
  проверкой stock/reservation. Принять отдельно delivery tariffs, адреса, упаковку,
  credentials и provider tests. До этого external writes false.

## Rollback

1. Выключить checkout/external writes и остановить workers/poller.
2. Сохранить redacted incident logs, counts и журнал внешних операций.
3. Для code-only ошибки вернуть совместимый прежний образ, оставить additive
   таблицы и новые заявки на месте; не удалять notification intents.
4. Для data migration восстановить проверенный backup/PITR в новую DB, сверить
   counts/sums и перенести/согласовать новые после backup заявки до переключения.
5. Rollback DB не отменяет внешние документы. Inflight/uncertain сверять по
   externalCode/syncId/provider IDs. Не удалять journal и не повторять POST до сверки.
6. После исправления — tests, schema check, staging E2E и отдельная production
   reactivation. Автоматический money downgrade не использовать.

## Integration references

[МойСклад API](https://dev.moysklad.ru/doc/api/remap/1.2/),
[СДЭК API](https://apidoc.cdek.ru/),
[Yandex claims/create](https://yandex.com/support/delivery-profile/ru/api/express/openapi/IntegrationV2ClaimsCreate),
[OSV API](https://google.github.io/osv.dev/api/). Provider acceptance tests остаются
отдельным этапом; отсутствие OSV findings не исключает любые возможные уязвимости.


## Release candidate worker (supersedes earlier missing-worker notes)

Railway staging worker: `ohmysmell-manager-bot-staging`, same feature branch/repo,
start `python -m app.bot.runtime`, APP_ENV=staging, BOT_ROLE=manager, one replica.
DATABASE_URL points to staging, EXTERNAL_WRITES_ENABLED=false, existing manager token.
Healthcheck `/health` is served on PORT; no public domain is needed. Response includes
status/role/polling/environment. During replacement, liveness may be 200 while the
new process waits for the old poller's session lock; polling=false is not readiness.
Configure overlap=0, draining=15 seconds. On shutdown polling/notification tasks stop,
the Telegram session closes and the DB lock releases. Connection loss or Telegram
conflict stops this process. Never run an unmanaged poller against the same token,
or use another DB to bypass ownership. Local staging_runner uses the same lock.

Notifications run in the manager worker, using the durable draft queue. No Gmail
worker is started. Sending is at-least-once: a crash after Telegram accepts a message
may repeat a notification, but buttons/finalize/order effects remain idempotent.
Inspect worker_ready/worker_stopped/worker_ownership_lost and duration_ms events.
API, client and manager handlers are separate; DB application services are shared.

Client worker is credential-ready, but needs a separate CLIENT_TELEGRAM_BOT_TOKEN.
Do not create a production bot service in this acceptance. Runtime deliberately
refuses APP_ENV=production unless BOT_PRODUCTION_ACTIVATED=true; only controlled
production activation may set that flag after deployment checks. External writes remain false.

Migration j93d5087bc10 follows i82c4f76ab09. Run fresh schema rehearsal,
existing staging upgrade and alembic check. Never stamp blindly. This migration
retains existing data; downgrade intentionally refuses audit deletion.

API tokens and bot tokens are secrets, never put in frontend or logs. Manager actor
header is accepted only behind internal authentication, not as end-user login.
The client bot requires its own token; no client action reaches manager handlers.
SQL logs and Telegram dependency exception logs are suppressed in worker runtime;
application errors record exception class, never connection strings or raw updates.

Noninteractive Railway CLI 5.52.1: environment edit reads JSON stdin before flags.
Pass an explicit patch through stdin and verify committed=true plus the effective
serviceManifest.deploy.startCommand/healthcheckPath. A zero exit code with
"No changes to apply" is not evidence of configuration. The staging worker must
show worker_ready, role=manager, polling_instances=1 and /health polling=true.
Reference: https://github.com/railwayapp/cli/blob/master/src/commands/environment/edit.rs

Checkpoint schema check: `python -m scripts.staging_check` is read-only and verifies
STAGING_DATABASE_URL differs from the local DATABASE_URL before connecting.

Current worker health endpoint (staging-only):
https://ohmysmell-manager-bot-staging-staging.up.railway.app/health
It exposes only readiness/role/environment/polling/write-guard state. The worker
uses the staging Postgres private-network reference; it has no application HTTP API.

DB recovery: async connections have a 10-second connect timeout and a 30-second
command timeout, in addition to the 15-second pool timeout. This bounds waits on
lost-network responses, which PostgreSQL statement_timeout alone cannot bound.
The caller receives the existing safe Russian recovery error; retry uses the same
idempotency key or a refreshed card. No blind retry of external requests is added.
