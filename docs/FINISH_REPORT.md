# OhMySmell — internal readiness completed

Дата: 2026-09-16. Ветка feature/sales-core-v2.
Проверенный application checkpoint: `888ce5626ec0a48d61ea21d4246b7058722ec007`.
Финальный documentation commit сохраняет application tree; local/GitHub/Railway HEAD
и health фиксируются в `.staging-artifacts/internal_final_receipt.json` после push.
Production/main не изменены. EXTERNAL_WRITES_ENABLED=false. Новые внешние credentials
не подключались, клиентский/email remote worker не запускались.

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

- Gmail OAuth отсутствует; readonly runtime/fakes готовы, нужен live account acceptance.
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
- [ ] Gmail credentials -> OAuth/live acceptance -> email activation.
- [ ] Exact Tilda origin/domain -> browser checkout acceptance.
- [ ] Separate client token -> live client acceptance.
- [ ] Delivery credentials/account configuration -> controlled live acceptance.
- [ ] Production secrets/infrastructure/deploy -> approved canary acceptance.
- [ ] Separately authorized external writes/provider canary.

Exact connection steps: [External integrations](EXTERNAL_INTEGRATIONS.md).
Deployment/config: [Production readiness](PRODUCTION_READINESS.md).
[Backup/restore](BACKUP_RESTORE.md), [Incidents](INCIDENT_RUNBOOK.md),
[PII/test data](DATA_RETENTION.md), [Staging evidence](REMOTE_STAGING_ACCEPTANCE.md).
