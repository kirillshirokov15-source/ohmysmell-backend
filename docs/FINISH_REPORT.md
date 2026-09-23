# OhMySmell — internal readiness completed

## Current Buying staging checkpoint — 2026-09-23

Implementation: `fa298d50ae18ba527003d765b734f87153d79e81` on
`feature/sales-core-v2`; migration head `m26a8310ef43`. Full local validation:
370 passed, 21 skipped, 7 subtests. PostgreSQL: 20 regression tests plus one quantity
E2E passed. Backend and manager staging deployment/health passed. **Email worker is
blocked by existing readonly Gmail OAuth `invalid_grant` (expired/revoked refresh
token), independently reproduced with both local and staging credentials.** No token
or scope was replaced. Exact manager group IDs and Sites CORS origin remain required.

Latest details and staging acceptance are in the Buying section at the end of this
report; frontend contract is [BUYING_API.md](BUYING_API.md). Earlier sections below
are historical checkpoints, not the current email-worker status.

Post-acceptance validation hardening rejects overlong normalized supplier names
(including expanding Unicode) during preview, before PostgreSQL mapping-key limits.

## Historical supply release validation — 2026-09-23

Only dependency adjustment: `anyio==4.14.1` -> `anyio==4.14.2`, explicitly authorized.
No additional feature changes. All 57 installed package versions match the pins.
Full pytest: 341 passed, 19 PostgreSQL tests skipped, 7 subtests passed. Compileall,
pip check, diff check and secret scan passed. OSV: 57 packages, zero findings.
Read-only staging Alembic current/check confirmed `k04e6198cd21`; fresh isolated
schema upgrade/check also passed. No operational staging migration was executed.

The complete PostgreSQL run finished with 1 passed and 18 failed in 720.98 seconds;
all 18 failures were `TimeoutError`. A separate isolated repeat of
`test_concurrent_finalize_creates_one_order` failed with `TimeoutError` during
`asyncpg.connect` -> `loop.getaddrinfo`, before SQL execution. An independent
read-only asyncpg probe without application services reproduced three connection
timeouts at 10.02 / 10.00 / 10.01 seconds. This establishes a connection-path
infrastructure failure, not a green PostgreSQL acceptance run; it does not identify
whether the fault is local networking/DNS or the Railway public connection path.
Release is authorized with this explicitly documented infrastructure exception.
Evidence: ignored `.staging-artifacts/supply-final-postgres.xml`,
`supply-postgres-isolated-retry.xml` and `supply-network-probe.json`.

Production/main unchanged; all three staging services have external writes disabled.
Read-only DB counters for external operations, MoySklad orders and external shipments
were zero. No supplier email or external write was initiated by this validation.

## Supply / procurement vertical slice — 2026-09-16

Implemented on `feature/sales-core-v2`; staging-only migration `k04e6198cd21`.
Seven additive tables: Supplier, ProductSupply, SupplierOffer, OrderItemSupply,
XSettlement, ProcurementRequest, SupplyEvent. Existing catalog product IDs are
reused; no duplicate product, physical external warehouse or fake own stock.

OWN behavior and email=wholesale / website=retail remain intact. X is exclusive
with OWN. X line calculation: base 200000 / sale 300001 -> partner margin 50000,
our margin 50001, partner due 250000 minor units. Below-cost X requires financial
review and has no automatic payable. PostgreSQL triggers protect financial history.

External USD acceptance: manager explicitly selected the second supplier offer,
4300 USD cents per unit × 2, manual 91.25 -> fixed cost 784750 RUB minor units,
sale 1000000 -> our margin 215250. Later offer/FX changes and process connection
restart preserved the snapshot. Concurrent selection/confirmation produced one
procurement and one audit event per effect. External email ingestion and website
checkout both finalized through their real application services using fake catalog
transport; supplier writes and supplier communication remained disabled.

Manager: source/finance sections in order card, «Требуют закупки», supplier selection,
requested/confirmed/received/unavailable/cancelled, CBR estimate and `/fx` override.
Received external items count toward assembly without a fictitious warehouse plan.
Order cancellation cancels its open procurements. Public API exposes availability
only; setup/finance endpoints require internal token plus active manager identity.

Validation: 341 unit tests plus 7 subtests; final PostgreSQL run recorded separately
in the acceptance evidence. Fresh schema upgrade/check and current staging
upgrade/current/check passed; compileall, diff check and credential scan passed.
Initial complete PostgreSQL run: 18 passed; additional external email/website
acceptance passed. Synthetic evidence lives only in ignored `.staging-artifacts`.

CBR live GET smoke passed without credentials. Explicit connect/read timeout 5/15 s,
bounded GET retries; no float money/FX. Initial supply card median 1821 ms dropped
below one second after replacing three SELECTs with one joined read (local-to-Railway;
Telegram network delivery is outside this measurement).

Boundaries: production/main/Tilda untouched; EXTERNAL_WRITES_ENABLED=false. Gmail
OAuth, selector, cursor and worker architecture were not changed. No supplier
email, purchase, payment, MoySklad document/warehouse or delivery write was made.
Real ownership mappings, suppliers/offers, FX-age policy and below-cost approval
policy remain business configuration/decisions. Details and setup API:
[SUPPLY_AND_PROCUREMENT.md](SUPPLY_AND_PROCUREMENT.md).

## Gmail channel policy and second live acceptance — 2026-09-16

Application commit `048852de6b5214d75ceff498d1abcdbffa006a83`.
Confirmed rule: **Email/Gmail -> wholesale; Website -> retail**, independent of the
existing customer profile. Conflicting profiles are preserved and recorded in
`contact_details.customer_type_policy`, structured lifecycle logs and manager card.
No Gmail customer-type confirmation; old type callbacks and API mutations are blocked.
Retail never falls back to wholesale pricing. Tilda itself remains disconnected.

New controlled message `1a0ab06fcdc683b8` was ingested by the Railway email worker:
**received=1, processed=1, failed=0** (3357.77 ms, including cold MoySklad reads).
Inbound **35**, draft **35**, existing customer **33**; the customer's unknown profile
was preserved while the draft became wholesale automatically. Status needs_review
only for unresolved product/counterparty, not customer type.

| Item | Qty | Match | Price minor | Item total minor |
|---|---:|---|---:|---:|
| MARV007 | 3 | matched | 56000 | 168000 |
| Chanel | 2 | ambiguous: CHNL010 first, CHNL018 second | null | null |

Draft total is null; no ambiguous product was selected. Manager card rendering shows
`Тип клиента: Оптовый`, `Источник: Email`, correct rubles, no customer-type buttons.
Previously delivered Telegram messages are snapshots: use «Обновить» or `/draft 35`
to render current UI. No manager callbacks were executed by acceptance scripts.
One notification outbox row is sent/attempts=1; replay did not send again.

Message historyId **339459**, persisted cursor **339525**, key
`gmail:faa7dd43e6d39a16be6937ceb8e7fd172f4619cc`. The first and repeated reads left labels
unchanged (including UNREAD). Direct pipeline replay returned the same draft. Railway
restart loaded the cursor and completed received=0/processed=0/failed=0 (193.48 ms).
No duplicate Customer/Draft/Order; total Order count remains 14.

Old controlled draft **34** was repriced through the application review service:
wholesale, revision1, MARV007=56000, Chanel unresolved. Customer profile unchanged,
no extra notification, no direct SQL data patch. Finalized orders were not rewritten.

**314 tests passed: 297 unit +17 PostgreSQL, plus 7 subtests.** PostgreSQL suite includes
retail-profile/email conflict, preserved audit snapshot, legacy reprice, channel callback
guard, website/wholesale-profile retail pricing and concurrent duplicate email.
Query tests cover history expiration, label arrival, delayed search indexing,
commit acknowledgement, selector intersection, query failure and restart replay.
Compileall, Alembic current/check, secret scan and diff check pass. No migration;
head remains `j93d5087bc10`. Backend/manager health/readiness=200, internal auth=401,
debug=404; email worker Online, one polling instance, no known secret leaks/tracebacks.

Mass intake **remains disabled**. Staging allowlist contains only the new test ID.
GMAIL_ORDER_QUERY is implemented but unset; query-only intake additionally requires
GMAIL_QUERY_INTAKE_ENABLED=true, currently disabled. Read-only audit of 25 headers could not
verify a deterministic business selector; the mailbox has zero custom labels.
Owner's “95% orders; plain questions without product/article/price are inquiries”
requires semantic inspection and does not define a safe Gmail search predicate.
Unparsed mail stays needs_review, cannot finalize, and its card says question or mail
without product lines. No automatic inquiry classifier is claimed.

Proposed future selector: `label:OMS-Wholesale-Orders after:<approved-Unix-epoch>`
(provider additionally enforces INBOX). Required from owner: exact dedicated label
or verified order alias, representative anonymized orders and inquiries, cutover time
and historical backfill policy, then approval of a read-only selection preview.
No label was created/applied by the integration. See EXTERNAL_INTEGRATIONS.md.
Production/main, Gmail writes and MoySklad/delivery writes remain untouched/disabled.
Ignored evidence: `gmail_wholesale_acceptance.json`, `gmail_wholesale_first_poll.json`,
`gmail_wholesale_preflight.json`, `gmail_legacy_reprice.json`, `email_remote_receipt.json`.

## Historical Gmail acceptance before channel-policy change — 2026-09-16

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
# Buying integration sprint — 2026-09-23

Implemented a working procurement workspace on the existing SupplierOffer domain:
revocable shared-password sessions, supplier-specific XLSX preview/atomic import,
canonical search/grouped offers, separate price history, persistent shared cart,
stale-price protection, deterministic supplier email previews, idempotent split
checkout and immutable purchase snapshots. Frontend contract:
[BUYING_API.md](BUYING_API.md); Excel details: [BUYING_PRICE_LISTS.md](BUYING_PRICE_LISTS.md).

Outbound supplier email and MoySklad procurement are explicit fake adapters plus
fail-closed live interfaces. No real supplier email or MoySklad write is performed.
Fake IDs are distinguishable, persistent and repeat-safe. Received actions create
one local audit event and a fake receipt reference. Gmail reply ingestion accepts
any sender/thread-matched reply, deduplicates message IDs, and supports readonly
thread polling in the existing email worker behind `SUPPLIER_REPLIES_ENABLED`.
Live sending still requires separate OAuth consent and reconciliation implementation.

Shared Telegram group access supports a chat allowlist and actual actor allowlist;
ordinary conversation is ignored. Existing private mode remains when group config
is absent. Buying group events use durable at-least-once delivery. Live group setup
is pending exact chat/user IDs. Gmail quantity extraction adds inline, table/HTML,
column and adjacent-line support, confidence/evidence, warnings, 1..5/manual
correction and actual actor audit. Unknown/probable quantities cannot finalize.
Mass customer Gmail intake remains disabled/controlled by existing selectors.

Migrations: additive `l15f7209de32` and `m26a8310ef43` (head). Fresh isolated PostgreSQL
upgrade and Alembic check passed; existing staging upgraded to this head with
current/check passed. No historical migrations were rewritten. Production/main
remain untouched. Buying credentials are configured only for staging backend and
stored locally in ignored `.env`; values are not printed or committed.

Local validation checkpoint: 370 passed, 21 skipped, 7 subtests passed. PostgreSQL:
20 passed in the full regression (including concurrent Buying checkout/send/received),
plus 1 passed in the separate quantity correction/finalize/replay scenario.
No DNS/connectivity exception occurred in these runs. Deployment acceptance results
are recorded below. Compileall, pip check,
secret scan and OSV audit (58 dependencies, zero findings) passed.

Limitations: XLSX only; values/formulas are not evaluated; incremental imports do
not deactivate omitted products. Existing catalog identities need explicit mapping
before import. CBR is a replaceable reference-rate fallback; USD can temporarily
have no RUB estimate until an FX refresh. Live Gmail sending and MoySklad writes
are intentionally unavailable. Exact Sites origin and manager group IDs are still
required for their live connection. Catalog/cart/details use bounded bulk queries;
performance measurements below are synthetic staging observations, not load-test p95.

## Buying staging acceptance

- Feature checkpoint pushed; Railway autodeployed backend and manager successfully.
- Backend `/health/db` 200; manager `/ready` 200 (polling=true, external_writes=false).
- Anonymous `/buying/catalog` returned 401. Buying secrets were provisioned through
  stdin only to the staging backend; plaintext values were never printed or committed.
- Synthetic HTTP E2E: supplier A+B, XLSX import, one canonical product/two offers,
  shared cart, preview, two purchases, fake sends, synthetic reply replay, details,
  received and fake receipt hooks passed. No real supplier email/MoySklad write.
- Initial HTTP observations: catalog 360 ms, cart 437 ms, preview 256 ms, confirm
  359 ms, purchase detail 306 ms; login cold request 1045 ms. No load-test claims.
- A real backend redeploy (`2b862a73-b7d8-4417-9074-0ab99e00a5ca`) completed SUCCESS.
  After restart the synthetic cart quantity and both purchases persisted; original
  checkout key returned the same IDs; repeated fake send/received preserved IDs and
  timestamps. Resume cart was then cleared. Post-restart cart 308 ms, replay checkout
  306 ms, purchase details 302–366 ms. The HTTP E2E artifact records
  `restart_verified=true`. These are synthetic procurement records only.
- Email deployment failed during unchanged readonly credential refresh. Both local
  and staging credentials have the same refresh token/client and independently
  return `invalid_grant`, expired/revoked. Reauthorization by the mailbox owner is
  required; mass intake and supplier sending were not enabled.
- Live shared-group delivery was not attempted without the actual chat/user IDs.
  Group filtering, actor audit, Buying/reply event delivery were tested with fakes.
- Artifacts (ignored locally): `buying-http-e2e.json`, `gmail-refresh-diagnostic.json`,
  `validation.json`, `staging-tests.xml`, `dependencies.json` under `.staging-artifacts`.
