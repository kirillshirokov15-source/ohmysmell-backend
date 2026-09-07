# Staging readiness audit

Audit started 2026-09-07 on `feature/sales-core-v2`. Production and external
business writes are excluded. Tests and migrations must use the project `.venv`.

## Findings and implementation sequence

1. Critical: public legacy checkout trusts customer type and can update an
   existing customer. Add a strict versioned public contract; protect legacy
   ingestion; preserve existing customer profiles.
2. Critical: finalize has no row lock; concurrent calls can create two orders.
   Review reads and saves in separate transactions and can overwrite concurrent
   edits. Lock mutations, invalidate old calculations, and compare review versions.
3. Critical: email cursor advances even when processing fails, losing retries.
   Preserve cursor on failures, isolate malformed messages, reject empty identities.
4. Critical: low-level MoySklad client bypasses service write guard. Guard every
   writing boundary, build deterministic export intents and payloads, never retry
   an uncertain external write automatically.
5. Missing: persisted warehouse allocations/shipments, delivery interfaces,
   checkout idempotency, channel-neutral ingestion schema.
6. Matching lacks exact code, empty-input protection and duplicate removal.
   Stock and stores pagination is incomplete; archived stores can be reintroduced.
7. Performance: repeated commit/get/review/get chains; synchronous catalog public
   route bypasses async cache. Add targeted timings and database integration checks.
8. Operations: hard-coded organization, fixed CORS, no deployment runbook, no
   Tilda contract. Add safe configuration and explicit activation/rollback gates.
9. Telegram menu still contains placeholder responses, errors are logged without
   manager feedback. Complete working lists and controlled error responses.

## Validation strategy

Run baseline and final full pytest, compileall, diff checks; regression tests for
business boundaries; real PostgreSQL tests in an isolated schema on verified
staging only; fresh Alembic upgrade and metadata check in that schema. Keep
staging E2E fixtures identifiable. Use fakes for writes and Gmail; read-only
MoySklad for live integration checks. Report unavailable evidence explicitly.

Final evidence and remaining activation gates will be recorded in FINISH_REPORT.
