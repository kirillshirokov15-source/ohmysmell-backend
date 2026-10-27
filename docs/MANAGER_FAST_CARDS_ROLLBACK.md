# Manager Fast Cards – deployment and rollback

Purpose: reduce display latency and remove redundant Telegram messages without changing payments, warehouse documents, role checks or other business rules.

## Scope
- The *only* service to deploy is `ohmysmell-manager-bot-production`.
- `OMS_MANAGER_FAST_CARDS_ENABLED=true` enables parallel **read-only** UI queries, in-place detail/counterparty cards, immediate order display after email draft confirmation, and callback progress toasts.
- `OMS_MANAGER_FAST_CARDS_ENABLED=false` reverts new UI behavior while keeping the existing `OMS_MANAGER_UI_V2_ENABLED` version and order operations unchanged.
- Enabling these flags must never enable another service or any MoySklad write permission.
- Existing shipped and paid orders, Tilda delivery states, auth, inventory reserve/dispatch and payment notes are untouched.

## Before rollout
Confirm a last-good snapshot of manager service source:
`rollback/manager-ui-v2-20261027-1813` → `41e75fe4efdcfb5f3415766622dec319b3ff8950`.
The source is pinned and can be redeployed if the new release cannot start.
Check `worker_ready` and `polling_instances=1` in the new manager deployment.

## Quick rollback
In Railway → project `eloquent-wisdom` → production → `ohmysmell-manager-bot-production` → Variables:
set `OMS_MANAGER_FAST_CARDS_ENABLED=false`, deploy, confirm `worker_ready`.
This preserves the previously deployed Manager UI v2 and no database edits are needed.

## Full rollback
In the manager service source only, redeploy the last good Railway build, or point that service to `rollback/manager-ui-v2-20261027-1813` pinned to the SHA above. Do not modify backend, Gmail workers, customer bot, Postgres, API tokens, Telegram allowlists, or MoySklad flags.

## Operational safety
The callback progress toast means processing started, **not** that payment, reserve or shipment succeeded. The success state is taken from the committed order record. No new confirmation-free path is added for warehouse reservations, cancellation, X receipt or shipping. Duplicate writes remain governed by existing database revision and idempotency rules. If the post-finalization read fails, the card still explicitly reports the confirmed order and offers a button to open it.

## Post-deployment checks
Verify fresh order cards and real Telegram callback navigation; compare `manager_order_callback.duration_ms` before/after for action=refresh and action=paid. User-visible responsiveness may also depend on Railway↔Postgres latency. Avoid performing real stock writes simply to time an operation.
