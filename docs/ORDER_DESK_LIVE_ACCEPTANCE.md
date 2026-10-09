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

## Resumed live acceptance: draft 36, finalized Order 15

The initial window ended before genuine user actions. A separately approved window
reused draft **36** and existing group card **176**, without creating another draft.
Both workers allowed only draft 36 and the approved private user/existing group;
the explicit message-ID scope was empty. Intake stayed disabled throughout the resume.
The expiring link was renewed and privately delivered with Outbox **5**, Telegram **4**.
`scripts.staging_order_desk_resume` checks this exact session/draft before every action;
the link sender uses the current encrypted delivery key instead of the original envelope.

Actual Telegram user actions and PostgreSQL audit confirm:

| Check | Evidence |
|---|---|
| Claim | manager 1, actor `898019732`, assigned timestamp; DeskEvent 3 |
| Client link | customer `898019732`, linked timestamp; DeskEvent 4 |
| Manager → client | Outbox 10, incoming group message 180, client receipt 8 |
| Client → group | Outbox 11, incoming client message 9, group receipt 182 |
| Order confirmation | DeskEvent 8; exactly one finalized Order 15, retail, 3030 minor units |
| Payment | OrderEvent 36, revision 1, paid |
| Assembly | OrderEvents 37/38, revisions 2/3, assembling → assembled |
| Manual courier | OrderEvent 39, revision 4, delivery method manual |
| Shipment status | OrderEvent 40, revision 5, shipped/dispatched |
| Delivery status | OrderEvent 41, revision 6, delivered |
| Final client `/status 36` | Outbox 22, client receipt 21, sent |

All conversation envelopes were sent in one attempt. Boolean checks against the two
approved synthetic text markers verified their routing without dumping message bodies.
Every human audit action resolves to the approved actor; no synthetic Telegram updates
were injected. The user confirmed completion of the genuine UI actions.
The authoritative Order statuses are paid/shipped/delivered. The separate desk workflow
stage retains `awaiting_payment`; it is not the source of truth for actual payment or delivery.

The first client command preceded link consumption and correctly failed ownership checks.
Its response is Outbox **7**, draft null, approved test recipient, pending, attempts 0.
It was excluded from the draft-only scope and was neither sent, deleted nor relabelled.
After linking, the repeated command produced exactly one customer message and receipt.
Global idempotency-key duplicate groups: **0**. At acceptance completion: **21 sent,
1 pending**, no sending/uncertain/failed/blocked; no unapproved recipients.
All draft-36 envelopes are sent. Draft 37 was not included in this window or changed.

Order 15 has `manual_fulfillment=true`, no MoySklad ID, and **zero Shipment rows**.
Global external operations remain **0**. No stock, procurement or real payment was created.

The full local regression rerun passed **531 tests + 7 subtests**, including 41 real
PostgreSQL tests in a fresh local schema. The fake-network lifecycle E2E passes and is
separate evidence from the actual Telegram acceptance above. Compileall, pip check, Alembic head/
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

Historical first-window verification at **2026-10-09 18:31 UTC** (superseded by the
resumed acceptance above and the final recovery artifact):

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

## Post-acceptance recovery, 2026-10-09 19:17 UTC

- Backend, manager and client raw variable collections exactly match their encrypted
  baseline, including original absence of temporary keys. Sending and intake are false;
  `EXTERNAL_WRITES_ENABLED=false`. Existing tokens and Gmail/MoySklad configuration match.
- All three deployments are SUCCESS; both workers report `worker_ready`, startup errors 0.
  Backend `/` and `/health/db` return 200; the actual disabled Tilda webhook returns 503.
- Queue remains **21 sent / 1 pending** (excluded Outbox 7); every draft-36 message is sent.
  No unapproved recipients, sending/uncertain/failed/blocked, or new external operations.
- Draft 36 points to paid/shipped/delivered manual Order 15. Draft 37 is unchanged.
  Shipment and procurement counts for Order 15 are both zero.
- Final regression: **531 passed + 7 subtests, 0 failed, 0 skipped**, including **41**
  PostgreSQL tests. Fresh local migration upgrade/check passed; staging migrations were
  not run. Compileall, pip check, secret scan and diff check passed. No linter is configured.
  Test artifacts: `acceptance-final-tests.xml`, `acceptance-final-migrations.log`,
  `validation.json`, all in ignored `.staging-artifacts`. The local test server is stopped.
- Live evidence: `.staging-artifacts/acceptance-resumed-live-evidence.json`; recovery
  evidence: `order-desk-approved-final-verification.json` in the same private directory.
  The latter is regenerated after deployment and is the current deployment inventory.

The application tree is unchanged after live acceptance; subsequent changes are the
operator resume/link-delivery tooling and this evidence report. Deploy final committed
snapshots with sending/intake off, then verify their revision before any feature push.

Push preflight found an enabled GitHub autodeploy for the staging email worker on
`feature/sales-core-v2`, with no watch-path filter. An ordinary push also redeploys that
worker. Do not assume a commit-message skip tag prevents Railway deployment: the
[documented control](https://docs.railway.com/deployments/github-autodeploys) is the service
autodeploy setting. Changing that unrelated worker's setting requires a narrowly approved
exception to the instruction not to touch Gmail. Its running deployment, variables and
credentials must remain unchanged. Production is connected to `main`, outside this task.

## Remaining real Tilda work

1. Obtain a synthetic payload from the actual Tilda project and confirm field/item mapping.
2. Supply verified external product IDs → existing catalog IDs and retail minor-unit prices;
   replace the temporary synthetic mappings. Missing retail price must stay in review.
3. Configure a trusted server adapter with the webhook secret outside the browser. For native
   query-secret mode, first verify redaction in all edge/proxy logs.
4. Implement checkout ownership verification and a trusted success-page bridge. Native webhook
   JSON response is not assumed to be available to the browser. Never expose an internal API token.
5. Keep stable transaction IDs across retries; test independent identical carts as separate orders.
6. Rehearse operational handling of blocked/uncertain delivery. Genuine link, claim,
   two-way text and the internal lifecycle have now passed; a second live manager and
   actual bot blocking were not exercised with the single approved test account.
7. Approve live intake/sending separately. Keep automatic Tilda→MoySklad sync, payments and
   warehouse document creation disabled; production remains outside this acceptance.
