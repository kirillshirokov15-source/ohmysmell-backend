# Remote staging — final internal readiness acceptance

2026-09-16. Application `888ce5626ec0a48d61ea21d4246b7058722ec007`.
Project eloquent-wisdom, environment staging, feature/sales-core-v2.
Final docs-only HEAD/health receipt: `.staging-artifacts/internal_final_receipt.json`.

| Service | Runtime |
|---|---|
| ohmysmell-backend-staging | FastAPI/Uvicorn only, /health + /ready + /health/db |
| ohmysmell-manager-bot-staging | python -m app.workers.manager_bot; /health + /ready; one replica; drain45 s |
| Postgres | Existing staging DB, private network for remote processes |

Backend: https://ohmysmell-backend-staging-staging.up.railway.app
Worker: https://ohmysmell-manager-bot-staging-staging.up.railway.app/health
Both application deployments SUCCESS. No client/email service created without credentials.
Production/main untouched. External writes false; no external operation rows or exported
Order/Shipment IDs. Existing manager notifications are permitted and processed.

## A — Manager

Synthetic inbound -> draft32 -> local Order14 -> full local warehouse allocation ->
manual delivery method -> assembling -> assembled -> shipped -> paid. Real API/DB
checks after every step: revision, amount, actor/timestamps, audit event count.
Finalize and each action replayed without duplicate effects; stale action rejected.
MoySklad guard returned disabled. Matching/catalog/stock use existing read-only access.

## B — Client

Real aiogram Update models -> separate dispatcher -> TelegramMethod calls handled by
fake BaseSession -> persistent staging DB. /start -> /request -> MARV007;1 -> email
-> /send -> draft33; unknown type/price remains reviewable. New dispatcher per update,
duplicate /send returns same request, only sender owns status. Manager API sees the
request and its email. No live client token or real client bot service claimed.

## C — Retry/restart/concurrency

Real deployment restart from 466fea5 to 888ce56: existing Order12 finalized again via
draft24 returns same ID; paid action replay preserves paid_at/revision and all five
audit rows. Unfinished website draft25 remains needs_review. PostgreSQL tests cover
cancellation before finalize commit (rollback then one order), engine disposal/restart,
two managers competing to pay, concurrent matching revision, distinct client /send
messages and identical update duplicates. Singleton lock rejects another session
without starting a competing poller. Worker stopped/ready events checked on rollout.

## D — Failure injection / API security

261 unit tests +14 real PostgreSQL tests (+7 subtests). Mocks cover DB unavailable,
MoySklad timeout/401/429/500, Telegram timeout/bad request/edit failure, stale callbacks,
email replay and crash before cursor save, expired Gmail history404, input/ownership,
insufficient stock, missing price/counterparty and unavailable/guarded delivery.
Gmail history expiry rescans INBOX including read messages. No cursor advance on
incomplete batch. External uncertain writes require manual reconciliation.

API acceptance on 466fea5: 60 requests/209 assertions, website draft25, email draft26,
local Order13. Later application changes are Gmail recovery/client polling ordering;
final A/B/C and final deployment smoke run on 888ce56 and its docs-only successor.
Public/internal boundaries, CORS, debug404, idempotency, tampering, integer money,
retail review and stock checks pass. New OpenAPI includes errors, enum status fields,
bounded IDs/query/pagination, and typed draft response schemas.

OSV: 57 pinned dependencies, 0 known findings. pip check, compileall, secret scan,
git diff --check green. Latest inspected backend logs:87 lines, ERROR0, traceback0,
api_failure0, TelegramConflictError0, known secrets0. Railway stderr INFO level
classification is not an application ERROR. One pending notification warning is an
expected handoff to the manager worker; queue completion checked separately.

## E — Backup/restore and migrations

Native pg_dump REPEATABLE READ snapshot of public:19 tables,259 rows. Restored into
new DB; row hashes, column metadata, sequences and head verified. Empty DB upgrade,
historical i82 restore+upgrade and Alembic check pass. j93 destructive downgrade
refused on disposable DB. Four generated test databases removed; live staging never
restored over. Independent synthetic fixture schema create/preview/cleanup passes;
unknown existing records untouched. See BACKUP_RESTORE.md for exact commands/hash.
Migration head remains j93d5087bc10; no new DB migration in this task.

## Repeatable performance

`python -m scripts.matching_benchmark`: five synthetic inbound matching requests,
shared synthetic customer, includes HTTP+DB+matching:3390.64,1131.54,1838.38,1222.66,
1428.38 ms; median1428.38 ms. Probe drafts27–31. Not consistently >3 s; no speculative
optimization or stock cache introduced. Product catalog cache remains TTL60 s.

Final A flow API: assembling563.04, assembled309.30, shipped388.14, paid294.05 ms.
Single initial matching3222.14 ms, warehouse allocation2394.60 ms (live stock read).
Local handler fake-send probe via public DB: menu1934/list2829/card6759 ms; this
is not remote worker user latency. Worker uses private DB. Prior accepted real
Telegram send/edit baseline inside Railway on 4d037be: menu390.94/list214.63/card282.06,
actions277.42–428.06/paid297.73 ms. Human callback acknowledgement not claimed;
manager send/edit were real, callback inputs synthetic. No live client measurement.

## Remaining gates and evidence

Only external account/configuration/origin/live acceptance and separately approved
production/write activation remain in scope. Credentials do not replace ordinary
per-order carrier tariff/route/package input. First live warehouse empty; two-store
split verified by tests. Missing retail price routes request to manager review.
Notifications at-least-once may duplicate a message after failure, never order effects.

Ignored evidence: release_candidate_e2e.json, restart_validation.json,
remote_acceptance.json, matching_benchmark.json, backup_restore.json,
staging-tests.xml, dependencies.json, remote_logs_summary.json, worker_logs_redacted.jsonl,
internal_final_receipt.json. Backups/PII/secret files stay outside git. No temporary SSH
keys were created for this continuation; prior acceptance keys had been removed.
