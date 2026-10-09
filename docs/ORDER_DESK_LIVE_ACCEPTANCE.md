# Approved synthetic staging acceptance

Scope: existing staging services only, Telegram user `898019732` and the existing
OhMySmell Work group. No live customers, Gmail, MoySklad documents or warehouse writes.

## Recovery tooling

`scripts.staging_order_desk_live_e2e` is an operator-only workflow for this explicitly
approved session. It does not run in deployed application images. Run with the project
`.venv` Python and Railway owner credentials; never export credentials into terminal output.

The `init` command verifies staging DB identity, manager allowlist/activity and actual
Telegram membership/group title. Original raw variables and temporary secrets are saved
with Windows user DPAPI in ignored `.staging-artifacts/order-desk-approved-session.dpapi`.
The file contains recovery secrets and must remain private. `observe` emits only sanitized
envelope metadata and internal statuses, never message bodies, contacts or tokens.

`configure` records every changed key before the mutation. Changes use stdin for secret
values and `skipDeploys=true`; deployment is a separate explicit action. Existing tokens,
Gmail/MoySklad settings and `EXTERNAL_WRITES_ENABLED` are not modified.

`enable` verifies every queued message in the exact synthetic draft scope, then permits
only the approved private user and configured group. The original checked message 1 is
explicitly selected; other non-draft messages are excluded. It does not authorize arbitrary
future `/help` or `/orders` acknowledgements without separately checking their IDs.

The one-time test link is delivered from encrypted session state using the client bot.
A scoped `DeskMessage` envelope records the attempt and Telegram receipt; its body contains
only a redacted description. The token is not persisted in plaintext in the DB or logs.
This operator link-delivery step is not a Tilda success-page bridge implementation.

Shutdown: `restore` first disables sending/intake and redeploys. After those deployments
complete, `restore-exact` verifies all unrelated variables against the baseline, atomically
restores the original variable collection (including originally absent keys) with
`skipDeploys=true`, and redeploys each service. No new unrelated settings may be overwritten:
the command aborts if an unrelated value changed. Verify deployments and queue afterward.
Do not use `redeploy --from-source`: the current reviewed image must be retained.

## Live webhook evidence

- Synthetic valid draft **36**: two products, quantities 2 and 1, retail total **3030 minor units**.
- Synthetic mismatch draft **37**: reported 3031, trusted total null, review required.
- Actual HTTPS form-urlencoded ingestion: 202; JSON replay: 200 with same draft ID.
- Two concurrent replays of draft 36: both 200, no additional order/card.
- Incorrect secret: 401. Empty cart: 422.
- No production data or real customer contacts used. External operations remain zero.

## Initial real Telegram receipts

| Outbox ID | Scope | Recipient | Telegram message ID | Result |
|---|---|---|---|---|
| 1 | Previously audited `/start` acknowledgement | Approved test user | 2 | sent, one attempt |
| 2 | Draft 36 new card | Approved existing group | 176 | sent, one attempt |
| 3 | Draft 37 review card | Approved existing group | 177 | sent, one attempt |
| 4 | Draft 36 one-time link | Approved test user | 3 | sent, one attempt |

Telegram API receipts prove acceptance by Telegram, not that the human read the message.
Actual user Start, claim, two-way text and lifecycle actions are evaluated separately from
transport receipts. Synthetic Telegram updates must not be substituted for those actions.

## Scope of completed acceptance

No genuine Start/link consume or manager claim arrived during the observation window.
Drafts 36 and 37 remain unassigned and unlinked; no finalized Order was created.
Consequently real two-way conversation, manager payment acknowledgement and assembly/
shipment/delivery transitions are **not yet verified live**. The operator requested Start,
`/message 36 SYNTHETIC CLIENT E2E`, the claim button and `/reply 36 SYNTHETIC MANAGER E2E`;
none may be simulated and reported as actual user interaction.

The full local regression rerun passed **531 tests + 7 subtests**, including 41 real
PostgreSQL tests in a fresh local schema. The fake-network lifecycle E2E passes, but is
separate evidence from this partial live acceptance. Compileall, pip check, Alembic head/
fresh upgrade/check, secret scan and diff whitespace checks pass. No staging migration
was run; head remains `p59db643bc76`. The local test PostgreSQL server was stopped.

The temporary intake was closed immediately after webhook tests (actual endpoint returned
503). Sending was then disabled before restoring the original variable collections.
The encrypted baseline preserves original absence of keys as well as values; restoration
does not leave synthetic product mappings or a permanent send allowlist behind.
The reviewed manager code remains deployed, as authorized; configuration restoration does
not roll code back to the version without recipient/scope guards.

Final sanitized evidence is stored locally in
`.staging-artifacts/order-desk-approved-session.json` and
`.staging-artifacts/order-desk-approved-final-verification.json`.
Do not interpret old test cards or the previous link as an open sending window after cleanup.
Resuming acceptance requires reopening the same bounded scope and checking link expiry.

Final verification at **2026-10-09 18:31 UTC**:

- All three raw variable collections exactly match the baseline; sending/intake false,
  `EXTERNAL_WRITES_ENABLED=false`; no token rotation or Gmail/MoySklad setting changes.
- Backend deployment `1efe068b-dbf8-4814-bee2-b9ca32d1375f`: SUCCESS.
- Manager deployment `df3dc8fe-cb4b-4f78-8560-77ecd5874b22`: SUCCESS, worker_ready, no startup errors.
- Client deployment `5ca56dda-45ae-460c-81cc-5bdeb6ca8005`: SUCCESS, worker_ready, no startup errors.
- Queue: 4 sent, 0 pending/sending/uncertain/failed/blocked; no unapproved recipients.
- Drafts 36/37: new, no manager, no Telegram customer, no finalized Order.
- External operations: 0; live Alembic head unchanged at `p59db643bc76`.
- Actual client message delivery was proven. The separate Railway HTTP healthcheck
  verification limitation from the go-live report is not resolved by a send receipt.

## Remaining real Tilda work

1. Obtain a synthetic payload from the actual Tilda project and confirm field/item mapping.
2. Supply verified external product IDs → existing catalog IDs and retail minor-unit prices;
   replace the temporary synthetic mappings. Missing retail price must stay in review.
3. Configure a trusted server adapter with the webhook secret outside the browser. For native
   query-secret mode, first verify redaction in all edge/proxy logs.
4. Implement checkout ownership verification and a trusted success-page bridge. Native webhook
   JSON response is not assumed to be available to the browser. Never expose an internal API token.
5. Keep stable transaction IDs across retries; test independent identical carts as separate orders.
6. Finish genuine Telegram acceptance and operational handling of blocked/uncertain delivery.
7. Approve live intake/sending separately. Keep automatic Tilda→MoySklad sync, payments and
   warehouse document creation disabled; production remains outside this acceptance.
