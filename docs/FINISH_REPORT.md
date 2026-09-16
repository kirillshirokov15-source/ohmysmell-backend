# OhMySmell — internal readiness completed

## Gmail live staging acceptance — 2026-09-16

**Passed.** Application commit `9f74540689d8bb170a78062fa92ef3a13a2f87eb`,
feature/sales-core-v2. Production/main/Tilda untouched; EXTERNAL_WRITES_ENABLED=false.
Account identity confirmed privately; no account address, OAuth value or message
body is recorded here. Scope: `https://www.googleapis.com/auth/gmail.readonly`.

- OAuth files exist outside the repository, are untracked, and their filenames are
  covered by .gitignore/.dockerignore. Scope validation + GET-only Gmail transport
  prevent send/modify/delete/archive. TLS verification remains enabled.
- Only controlled message `1a0aad59fceb36b1` was ingested. It was already read;
  labels remained unchanged. Allowlist applies to bootstrap, history and recovery.
- Real provider/parser/customer/matching/price/counterparty/draft path used the
  verified staging DB. First run: received=1, processed=1, failed=0.
  Inbound **34**, draft **34**, customer **33**, status **needs_review**,
  customer_type **unknown**, counterparty absent (0 candidates).
- Chanel qty=2 remains ambiguous: CHNL010 first, CHNL018 second; no silent selection.
  Marvis qty=3 matched MARV007. Draft prices/total are null because customer type is
  unknown. Separate read-only pricing check returned wholesale **56000 minor units**;
  retail missing-price handling did not fall back to wholesale.
- Message historyId **339416**; saved account/selector cursor **339454** under
  `gmail:7946a9349bce24507fc6d322b2b1643f21a3172c`. No manual cursor UPDATE.
- Next poll received=0, processed=0, failed=0. Direct replay through the same ingestion
  service returned draft34. Inbound/draft/customer counts increased once only; Order
  count stayed 14. Relevant PostgreSQL unique constraints checked.
- One immediate notification to the single active manager succeeded; outbox remained
  sent/attempts=0 across retries/restarts. Card/keyboard checked, no callbacks performed.
  This proves one send in this run, not exactly-once Telegram delivery under every
  possible crash (notification outbox remains at-least-once).
- Two independent local runtime starts loaded the saved cursor, completed empty polls,
  excluded a second advisory-lock owner and stopped through the installed signal
  handler. Railway redeploy also logged worker_stopped and resumed empty polling.
- **ohmysmell-email-worker-staging Online**, service
  `56306c0e-ff3a-4b34-89e7-856c27d76da5`, one replica, explicit email_runtime entrypoint,
  staging private Postgres reference, no Telegram token, writes=false. OAuth secret env
  reconstructs private files at boot; secret transfer and resolved DB identity verified.
  Deployment healthcheck /health permits singleton handoff; /ready requires successful
  recent polling. Remote polls measured 114–213 ms (empty batches, not parsing latency).
- Backend/manager /health and /ready: 200. Internal request without auth: 401;
  debug endpoint: 404. Remote logs: no tracebacks or known secret matches.
- **295 tests passed: 281 unit +14 PostgreSQL, plus 7 subtests**. Includes revoked/expired
  token, 401/429/500 recovery, history expiration, selector isolation, token scope,
  blocked writes, secret reconstruction and existing crash/replay regressions.
  Failures used mocks; the real account/token was not deliberately broken.
  Compileall, Alembic current/check, secret scan, diff check passed. Head remains
  `j93d5087bc10`; no migration needed.

Controlled Gmail acceptance has no remaining blocker. Remote worker intentionally
polls ONLY the accepted message ID. Normal mailbox intake is not enabled; expanding
the allowlist requires approval of the next controlled message(s). A second email was
unnecessary to prove persisted history polling/restart/idempotency.
Ignored local evidence: `.staging-artifacts/gmail_*.json`, `email_*.json`.
Final deployment SHA is verified after documentation push.

## Historical internal-readiness checkpoint

Дата: 2026-09-16. Ветка feature/sales-core-v2.
Проверенный application checkpoint: `888ce5626ec0a48d61ea21d4246b7058722ec007`.
Финальный documentation commit сохраняет application tree; local/GitHub/Railway HEAD
и health фиксируются в `.staging-artifacts/internal_final_receipt.json` после push.
На этом историческом checkpoint production/main не изменены, external writes=false;
Gmail live acceptance и remote email worker добавлены позже и описаны выше.

## Результат

Внутренние задачи заявленного scope закрыты. Следующие этапы — подключение аккаунтов,
credentials/domains и live acceptance, затем отдельно разрешённая production/write
activation. Это не утверждение о проведённой production или live Gmail/client приёмке.

- Manager workspace: списки/поиск/карточка, review/matching/counterparty, склады,
  сборка/отгрузка/оплата/проблема/отмена/refresh, контекстные русские кнопки и ₽.
- Fulfillment/payment/delivery независимы от technical Order.status. Actor, timestamp,
  audit, revision, durable idempotency. Два менеджера не проводят двойную оплату.
- Client: отдельный router/entrypoint/token, persistent dialogue, own-status only,
  manager contact, duplicate submit safety. Реальные модели aiogram Update и методы
  проходят fake BaseSession. Email контакта больше не теряется в заявке.
- Client polling последовательный: порядок диалога сохраняется, offset продвигается
  после завершения handler. Manager имеет bounded concurrent handlers и DB locks.
- Backend, manager, client, email запускаются отдельно. Email получил singleton,
  health/readiness, signal shutdown, bounded HTTP и cursor recovery. Истёкшая Gmail
  history сканирует INBOX, включая уже прочитанные письма; unique IDs убирают повторы.
- Process-specific validation: Gmail/client token не мешают backend; workers не
  требуют чужой internal API/CORS конфигурации. Все production activation gates явные.
- Liveness /health отдельно от /ready. Нет catalog fetch в readiness. Structured
  ERROR hooks и optional Sentry готовы; monitoring выключен, telemetry не отправлялась.
- API: bounded IDs/pagination/query, response/status/error schemas и OpenAPI;
  неожиданные ошибки перехватываются до утечки traceback с параметрами DB/provider.
- Native backup/restore, migration rehearsal и manifest-protected fixture cleanup
  выполнены. Retention/PII, incident и connection runbooks подготовлены.

## Проверки

**275 tests passed**: 261 unit +14 PostgreSQL; дополнительно 7 subtests.
В обычном pytest 14 staging tests пропускаются и затем проходят отдельным запуском;
они не посчитаны дважды. Последний полный PG run: 392.33 s, 14/14.
Compileall, Alembic current/check, fresh/historical/restored migrations, secret scan,
git diff --check и pip check пройдены. OSV: 57 pinned packages, известных findings=0
на момент проверки. Новых миграций не потребовалось: head `j93d5087bc10`.

Staging A/B/C: draft32 -> Order14 -> assembling -> assembled -> shipped -> paid;
client fake transport -> draft33 -> manager visibility/own-status; replay/stale checks.
После настоящего redeploy: Order12/draft24 finalize и paid повторены, paid_at и 5 audit
events не изменились; незавершённый draft25 сохранился. Полный API acceptance:
60 HTTP requests /209 assertions. Failure injection: 401/429/500/timeout, DB failure,
Telegram timeout/bad request, stale/duplicate callbacks, malformed inputs, stock/
price/counterparty/delivery failures; recovery не повторяет committed effects.

Backup: 19 таблиц/259 строк из согласованного staging snapshot восстановлены в новой
DB; hashes/columns/sequences/head совпали. Empty DB -> head; restored historical
`i82c4f76ab09` -> head с сохранением данных. Четыре disposable DB удалены. Destructive
downgrade j93 отказал ожидаемо только в test DB. Действующая staging DB не была целью
restore. Отдельная synthetic schema создана, проверена и удалена по manifest+marker.

## Performance

Пять одинаковых ingestion+matching probes через staging API:
3390.64 /1131.54 /1838.38 /1222.66 /1428.38 ms; median **1428.38 ms**.
Это includes network + DB + matching, не p95. >3 s не стабильно, поэтому новых
оптимизаций/ослабления stock freshness не внесено. Финальный operational API E2E:
assembling 563 ms, assembled309 ms, shipped388 ms, paid294 ms.
Предыдущие actual Railway Telegram send/edit measurements: menu391, list215, card282,
actions277–428, paid298 ms. Они сохранены как baseline, не выданы за новый human tap.
Локальные handler probes через публичный DB network значительно медленнее; remote
worker использует private DB network. Подробности в REMOTE_STAGING_ACCEPTANCE.md.

## Границы и оставшиеся подключения

- Gmail readonly live acceptance пройден; staging email worker работает с одним
  разрешённым message ID. Production mailbox activation — отдельное решение.
- Tilda отсутствует; нужен exact origin/domain и browser checkout acceptance.
- Отдельного client token нет; код/runtime/transport/PG tests готовы, нужен live bot.
- Delivery adapters и operator validate/prepare/submit command готовы. Нужны credentials
  и обычные данные конкретной доставки: тариф/маршрут/упаковка/контакт. Автоматический
  тарифный магазин, платежи и blind courier dispatch не входят в этот scope.
- Нет retail price: controlled review работает, wholesale клиенту не показывается.
  Priced retail требует утверждённой price mapping. Первый live склад пуст;
  deterministic two-store split проверен через fakes/PG, live split не заявлен.
- Notifications at-least-once: возможен повтор уведомления после аварии, не перехода.
- Production secrets/infrastructure/live acceptance и внешние writes не активированы.

## Exact final checklist

- [x] Manager daily workflow/русский UX/контекстные действия.
- [x] Client router, entrypoint, persistence, restart, own-order access, fake contract.
- [x] Independent worker configuration/lifecycle/singleton/readiness.
- [x] Recovery/chaos/concurrency и failure injection.
- [x] Native staging backup + disposable restore + integrity.
- [x] Empty/historical/current migrations и rollback strategy.
- [x] Safe HTTP timeout/retry, external operation reconciliation policy.
- [x] Disabled monitoring hooks и secret/PII-safe errors/logs.
- [x] API/OpenAPI/auth/input/role boundaries.
- [x] Synthetic fixtures/preview/owned cleanup и retention policy.
- [x] Unit/PG/E2E/dependency/security checks и staging deploy.
- [x] Per-process production template, incident/runbooks, connection checklists.
- [x] Gmail credentials -> OAuth/live staging acceptance -> isolated email worker.
- [ ] Exact Tilda origin/domain -> browser checkout acceptance.
- [ ] Separate client token -> live client acceptance.
- [ ] Delivery credentials/account configuration -> controlled live acceptance.
- [ ] Production secrets/infrastructure/deploy -> approved canary acceptance.
- [ ] Separately authorized external writes/provider canary.

Exact connection steps: [External integrations](EXTERNAL_INTEGRATIONS.md).
Deployment/config: [Production readiness](PRODUCTION_READINESS.md).
[Backup/restore](BACKUP_RESTORE.md), [Incidents](INCIDENT_RUNBOOK.md),
[PII/test data](DATA_RETENTION.md), [Staging evidence](REMOTE_STAGING_ACCEPTANCE.md).
