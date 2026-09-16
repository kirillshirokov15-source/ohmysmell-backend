# Incident runbook

All current commands target staging; production actions require separate approval.
Never enable external writes as a recovery step.

| Symptom | Inspect | Safe recovery |
|---|---|---|
| Backend 503 | /health then /ready and /health/db; api_failure result | Restore DB/network; retry same idempotency key, check order before new input |
| Worker /ready 503 | /health status; worker_starting/ready/stopped | Check token/env/DB and singleton owner; stop old deployment, then redeploy one replica |
| TelegramConflictError | Other host/process using same token | Stop duplicate; do not fight polling or delete webhook automatically |
| MoySklad 401 | Token permission/account; never print response/token | Replace token; do not retry auth failures automatically |
| MoySklad timeout/429/5xx | moysklad_call_completed duration/outcome | Bounded safe GET retries; retry review/match; never blindly retry writes |
| Old callback | Revision changed | Refresh card; no mutation from expired/versionless callback |
| Send/edit timeout | Check DB/action audit | Refresh/status; saved action survives Telegram failure |
| Email degraded | email_poll_failed / email_ingestion_error | Cursor stays at last complete batch; restart replays unique IDs |
| Invalid email keeps batch pending | Failed message_ref, authorized provider/DB inspection | Correct source/parser input under review; never skip a cursor blindly |
| External operation inflight/uncertain | external_operations key + provider document | Reconcile by stable externalCode/request_id; no automatic resend |
| Migration failure | Recorded version + transaction result | Stop rollout; restore rehearsal/forward repair; never blind stamp |

`/health` is cheap process liveness. `/ready` requires DB for API; Telegram requires
poller ownership and startup checks; email additionally requires a successful batch
within max(180 s, 3 poll intervals). Neither probes the full MoySklad catalog.
Railway uses /health to avoid deadlock while a replacement waits for the previous
singleton lock to drain; external monitors should alert on /ready and degraded logs.

Worker termination: SIGTERM stops polling, closes HTTP/DB sessions and releases
session advisory ownership. Email allows 25 s for an active batch, then cancels;
HTTP timeout is 20 s. Set drain >=45 s. Hard kill releases PostgreSQL connection
locks; atomic transactions roll back, committed requests replay safely.

Alert hooks (no provider activated): ERROR api_failure, worker_startup_failed,
email_poll_failed, email_ingestion_error, failed callbacks;
worker_ownership_lost event; health/readiness failure 2 minutes; repeated latency >1 s manager actions or >3 s
matching. Catalog latency is measured separately. Notification queue oldest pending
>5 minutes should alert; delivery is at-least-once, duplicate messages are possible.
Optional Sentry is preinstalled but MONITORING_ENABLED=false; no DSN/network now.
