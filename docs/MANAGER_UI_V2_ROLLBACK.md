# Manager Bot UX v2 – activation and rollback

This is a **reversible feature flag**, not an authentication backdoor. All Telegram actor allowlists, active `managers` records, role checks and MoySklad write guards remain unchanged.

## Scope

Only the production **manager bot** needs an updated source. Do not change the backend, client bot, Gmail workers, database schema or warehouse settings.

Feature branch: `feature/manager-ui-navigation-v2-20261027`
Production snapshot / rollback branch: `rollback/manager-ui-prod-20261027`
Original manager production commit: `a63f9f62d017a2533d388e17f2ca624f4cfdef84`

## Activate

1. Railway project `eloquent-wisdom` → **production** → `ohmysmell-manager-bot-production`.
2. Deploy the feature branch only to this service.
3. Set `OMS_MANAGER_UI_V2_ENABLED=true` in this service.
4. Confirm one healthy manager poller (`worker_ready`, `polling_instances=1`) and check /start in the existing group.
5. Check a shipped+paid **email** order: compact receipt, **Подробнее**, **Главное меню**.
6. Check a shipped+delivered **Tilda** order: compact receipt and navigation.
7. Check an assembled X-warehouse order: only explicit read-only checks in the main flow. Do not create a real reserve to smoke-test navigation.

Existing old Telegram messages do not disappear automatically. Their content changes when the manager refreshes the corresponding card.

## Immediate rollback – no code change

In Railway production manager service only:

- Set `OMS_MANAGER_UI_V2_ENABLED=false`, deploy and confirm `worker_ready`.
- The pre-existing menu and order-card presentation resume, without changing order, payment or stock records.

## Full source rollback

If the new deployment fails to boot, reconnect **only** `ohmysmell-manager-bot-production` to
`rollback/manager-ui-prod-20261027` (commit `a63f9f62d017a2533d388e17f2ca624f4cfdef84`), or redeploy the last known-good Railway deployment.
Confirm `worker_ready`; keep DB and all other services untouched.

Do not change `MANAGER_TELEGRAM_USER_IDS`, `MANAGER_TELEGRAM_CHAT_ID`, `TELEGRAM_BOT_TOKEN`, `MOYSKLAD_TOKEN` or any warehouse write flag during rollback.

## Boundaries

- This release **does not** modify business fulfillment, X-warehouse writes, reservations, shipment workflow or price calculation.
- A closed order is visually compacted only if Gmail is both shipped and paid, or Tilda is shipped and marked delivered.
- The "reserved" action remains available in additional warehouse operations with an explicit warning; it can create real MoySklad records.
- This feature flag controls **presentation only**, never permissions.
