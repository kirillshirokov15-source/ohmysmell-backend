# OhMySmell: integrated staging release candidate

## Scope and provenance
This branch starts from the current Manager Bot production tree plus the reviewed guided UI in draft PR #5.
The following changes were deliberately integrated **by file/behavior**, not by merging outdated feature branches:

- PR #3: Client Bot PostgreSQL lease/watchdog fail-closed recovery and tests.
- PR #4: customer Gmail worker transient authentication transport refresh, preserving cursor, supplier mailbox and OAuth secrets.
- PR #5: Manager Bot button-first counterparty, product and quantity input with actor-scoped ForceReply; Davian/Давиан searches remain **unverified suggestions**.
- PR #2 (partial): durable per-chat, per-entity Telegram card receipt models, sending journal, safe old-card retirement, optional client/card callback guards, additive Alembic migration, p50/p95 instrumentation for card publishing, operator manual reconciliation.

The production Client Bot checkout-item title behavior (Tilda original text, no internal MySklad product name) is retained via `tilda_product_lines` and a read-only `public_summary`.

## Safeguards

**Never enable by default:** `OMS_TELEGRAM_DURABLE_CARDS_ENABLED=false` (the default if unset).
The flag changes actual send/delete behavior. Before setting true in any live Telegram worker:
1. Back up PostgreSQL and verify the migration graph. Apply `alembic upgrade head` only in the intended environment. Migration `s82ae976fc09` adds `telegram_cards` and `telegram_card_attempts`.
2. Limit test recipients and isolate the staging manager Telegram bot/token from production; ensure the same bot and chat are not polled by two workers.
3. Verify dry-run/staging external writes remain off; use fake MySklad providers for tests. Protect active client messages, manager discussions, and older cards without receipts.
4. Run read-only integration tests and a **supervised** Telegram transport trial, recording p50/p95 ack and publish latencies.
5. Handle any uncertain send through operator `/cardreceipt draft:ID` or `order:ID`, replying **to the exact original message from this bot**. Never blindly retry or manually enter a guessed message ID.
6. Release to production only after green CI, successful live Telegram smoke, migration preflight and a tested rollback to flag false.

CI/test-only staging manager service currently starts `python -m app.workers.hatuna_readonly_smoke`, not a Telegram poller. Healthy Railway deployment alone therefore cannot prove callback latency.

## Remaining functionality **not** automatically merged from PR #2
- `own_x` versus existing `partner_x`: do not import migration `t93bf087ad10` or change stock types until business rules and stored allocations are reconciled with the already deployed X workflow.
- Unknown-SKU `identity_required`, early commercial approval and source preselection: require focused compatibility work with newer Tilda/manual MySklad mappings. An unknown product must never result in real receipt/reserve/shipment before its identity is established.
- Full card receipt synchronization with every independent source-navigation and service-card producer, including existing `DeskMessage` outbox cards, still requires live staging validation.
- Live Telegram p95, real MySklad document write tests, supplier Gmail OAuth and Buying end-to-end flow remain outside this safe noninteractive test.

## Rollback and release boundary
Switch `OMS_TELEGRAM_DURABLE_CARDS_ENABLED=false` to disable the durable transport without deleting receipt tables. Do **not** use `alembic downgrade` on production: delivery audit records must remain. No production variables, write flags, schema or services are changed by this GitHub branch.
