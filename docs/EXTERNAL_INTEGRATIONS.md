# External integration connection checklists

Supply/procurement: [SUPPLY_AND_PROCUREMENT.md](SUPPLY_AND_PROCUREMENT.md).
CBR FX is a read-only GET adapter with no credentials, explicit timeouts and bounded
retries. Manager explicitly refreshes an estimate or enters a manual rate before
confirmation. No currency operation/payment occurs. SupplierCommunication is an
interface only; no supplier email is sent. External bucket is local, never a real
MoySklad warehouse. OWN stock and Gmail channel pricing are unchanged.

Gmail controlled read-only staging acceptance and remote worker restart are complete
(2026-09-16), with evidence in FINISH_REPORT.md. Other external
integrations below remain disabled. Production/main untouched.
Keep EXTERNAL_WRITES_ENABLED=false until a separate write activation decision.
Commands run in the intended service environment; never copy the staging DB into
production variables. Application code and entrypoints are already provided.

## Gmail (read-only)

- Credentials: Google OAuth desktop client JSON + authorized user token JSON with
  exactly https://www.googleapis.com/auth/gmail.readonly; owner consent required.
- Env: GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, GMAIL_USER_ID=me,
  GMAIL_INITIAL_QUERY, GMAIL_ALLOWED_MESSAGE_IDS, GMAIL_ORDER_QUERY,
  EMAIL_POLL_INTERVAL=60, DATABASE_URL, APP_ENV;
  MOYSKLAD_TOKEN/price mapping for product matching. Production additionally
  EMAIL_PRODUCTION_ACTIVATED=true after approval. Internal API token/CORS not needed.
- Put client JSON outside the repository in a restricted directory; run
  `python -m app.scripts.gmail_oauth` once in an interactive owner-controlled session.
  Transfer authorized token without printing it. Runtime may refresh/rewrite that file.
- Railway alternative (implemented): GMAIL_CREDENTIALS_JSON_BASE64 and
  GMAIL_TOKEN_JSON_BASE64 are SECRET variables containing base64 of the respective
  JSON files. Upload via Railway Variables UI or CLI `variable set KEY --stdin
  --skip-deploys` with captured output, never values in arguments. Set paths to
  /tmp/ohmysmell-gmail/gmail-credentials.json and /tmp/ohmysmell-gmail/gmail-token.json.
  Runtime validates JSON/scopes and reconstructs missing files with mode 0600 inside
  a 0700 directory. Existing refreshed token files are preserved. Ephemeral restart
  reconstructs from the secret containing the refresh token; rotating/revoking OAuth
  requires updating that secret. Base64 is encoding, not encryption; protect it as a token.
- Controlled staging uses GMAIL_ALLOWED_MESSAGE_IDS (comma-separated IDs). The same
  allowlist applies to first intake, history polling and expired-history recovery.
  GMAIL_INITIAL_QUERY alone is NOT a history filter. Current acceptance permits only
  one controlled message; expanding intake requires a separately approved selector.
- Business rule: email drafts/orders are always WHOLESALE, including when an existing
  Customer profile is retail/unknown. The profile is preserved; conflict snapshot is
  durable in draft contact_details and shown in manager card. Matched items get
  wholesale prices immediately. No manager type confirmation. Website is always RETAIL.
- Prepared optional GMAIL_ORDER_QUERY is an additional Gmail search predicate, enforced
  within INBOX on bootstrap, normal polling and expired-history recovery. If IDs and
  query are both present they intersect. Query-only intake in staging/production
  requires GMAIL_QUERY_INTAKE_ENABLED=true explicitly. Otherwise a missing ID allowlist
  fails startup. The flag defaults false; current staging does not enable it.
  Query failure fails closed; it never falls back to the whole inbox. Selector change
  gets a new account/selector cursor. Query text is configuration, never logged.
- Query mode reconciles matching IDs each poll to tolerate delayed search indexing and
  labels added after arrival. Only committed IDs are cached in memory; failed messages
  retry and restart replays through durable DB uniqueness. Steady polls list IDs but
  do not re-download acknowledged bodies. Keep the selected population bounded with
  an approved cutover date/label and monitor polling duration. Gmail history continues
  to persist normally. Gmail labels/filter creation remains the mailbox owner's job.
- Selector audit (2026-09-16): bounded read-only sample of 25 INBOX headers, 7 sender
  domains, 18 subjects, 10 distinct recipient header strings (not verified aliases),
  no custom labels in that sample. Twelve subjects contain order/заказ, including
  controlled tests. This does NOT verify any sender/domain/subject/recipient as a
  safe production selector. Historical bodies were not ingested or logged.
  A separate readonly labels.list audit confirmed zero custom labels in the mailbox;
  the proposed OMS-Wholesale-Orders label does not exist yet.
- Owner says about 95% are orders, and a question without product/article/price is
  an inquiry. That is a semantic content rule, not a reliable Gmail search predicate.
  A question may name a product; an order may have no price. Do not filter solely on
  a question mark, price, sender frequency or the word order. Unparsed mail remains
  needs_review with no finalizable Order; this is NOT an automatic inquiry classifier.
- Proposed conservative selector, NOT activated:
  `GMAIL_ORDER_QUERY=label:OMS-Wholesale-Orders after:<approved-Unix-epoch>`.
  Owner first confirms/applies a dedicated order label (or supplies a dedicated order
  alias with verified examples); integration never adds labels or marks read.
  Need representative anonymized orders AND inquiries, the exact label/alias, the
  cutover timestamp/backfill policy and approval of the selection preview. Until then
  retain the controlled ID allowlist; do not enable unfiltered INBOX intake.
  Once approved: set the exact query, set GMAIL_QUERY_INTAKE_ENABLED=true, remove the
  test-only IDs and redeploy only the intended email worker. Observe the selected
  canary/restart/cursor before expanding further. Roll back by restoring the test IDs
  and flag=false or stopping email service. Production also requires its own separate
  activation gate; enabling a query never enables external writes.
  Gmail search/history contracts: [messages.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list),
  [history.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list).
- Runtime derives a cursor key from account + selector (hashed, no email in logs).
  Existing legacy `gmail` cursor is left intact. A changed selector starts its own
  cursor; durable external message IDs still deduplicate. One account per DB remains
  the supported topology. Do not manually reset cursors.
- Smoke: `python -m app.scripts.gmail_smoke_test`; confirm account privately and no writes.
  Launch `python -m app.workers.email_runtime`, one replica, /health and /ready.
  Send one test order email, verify one draft, restart, verify unchanged draft ID
  and saved cursor. History 404 with an allowlist only re-fetches those IDs. Without
  an allowlist, recovery rescans INBOX including already-read messages.
- Gmail HTTP transport permits GET only. Stored token scope must be exactly readonly.
  Requests have a 20-second timeout and verified TLS; explicit OAuth refresh uses
  bounded connect/read timeouts. Failed polls retain cursor and retry at the configured
  interval (60 seconds), without a tight retry loop. Revoked authorization fails startup
  or makes readiness unhealthy; reauthorize deliberately, never modify the live token
  to simulate failure. Local fakes cover expired/revoked token and 401/429/500.
- Email service has NO Telegram token. The manager worker delivers its durable
  notification outbox. Backend and manager do not start Gmail polling. One replica,
  PostgreSQL advisory ownership, overlap=0, drain=45 seconds, /health and /ready.
  Use /health for Railway deployment gating so a replacement can wait for ownership;
  /ready stays unhealthy until its first successful poll. Alert on /ready separately.
- Current staging service: ohmysmell-email-worker-staging, Online, one replica.
  Latest accepted message -> inbound35/draft35, automatically wholesale, MARV007=56000;
  Chanel remains ambiguous. Cursor persisted, replay/restart deduplicated. Previous
  controlled draft34 was repriced through review without changing the customer profile.
  Leave its current message allowlist intact until additional intake is authorized.
- Disable: stop only email service; preserve token/cursor, revoke OAuth if compromised.

## Tilda

- No backend secret belongs in Tilda. Need exact final HTTPS page origin/domain.
- Env: backend CORS_ORIGINS=exact origin(s), PUBLIC_CHECKOUT_ENABLED=true;
  MOYSKLAD_RETAIL_PRICE_TYPE only when an approved retail price exists.
- Follow TILDA.md payload contract. Use /api/v1/catalog and /api/v1/checkout only,
  stable Idempotency-Key per unchanged checkout attempt; do not send a trusted price
  or privileged status. Missing retail price remains a review request.
- Smoke from final browser: preflight, catalog, checkout, duplicate click, changed
  payload/key conflict, unavailable stock, review response and manager visibility.
- Disable: PUBLIC_CHECKOUT_ENABLED=false, remove origin, redeploy backend.

## Client Telegram

- Need a NEW bot token, different from the manager token.
- Env: CLIENT_TELEGRAM_BOT_TOKEN, DATABASE_URL, APP_ENV; production additionally
  BOT_PRODUCTION_ACTIVATED=true. No manager token, Gmail, internal token or CORS needed.
- Use deploy/worker-services.json client section; start
  `python -m app.workers.client_bot`. One replica, no webhook, no other local poller.
  Token config does not start anything in FastAPI.
- Smoke: /start -> /request -> `Свеча; 2` -> own contact or email -> /send -> number;
  /status number -> /manager. Restart at contact step and resume. Repeat /send.
  Another account must not access that number; manager callbacks must be rejected.
  Prices are confirmed by manager; wholesale price never appears.
- Disable: stop client service/revoke its token. Manager service unaffected.

## MoySklad writes

- Need write-capable MOYSKLAD_TOKEN, MOYSKLAD_ORGANIZATION_ID, correct warehouse IDs
  and existing verified counterparties. These are production account settings.
- First verify read-only catalog/stock/price/account configuration with guard false.
- After explicit approval set EXTERNAL_WRITES_ENABLED=true ONLY in the intended
  production process. Staging validation always rejects this flag.
- Controlled canary: authenticated POST /orders/{id}/moysklad for one reviewed order;
  inspect externalCode, minor-unit sum, counterparty and exactly one document.
  Repeating the same operation must return the existing result, not a second write.
  Shipment export is separate; use ShipmentExportService only after canary approval.
- Disable: set flag false/redeploy. Do not delete created business documents as rollback.
  inflight/uncertain operations require provider reconciliation; do not reset journal
  records to force a retry. Restore only after accounting for provider-side effects.

## CDEK

- Credentials: CDEK_CLIENT_ID, CDEK_CLIENT_SECRET for the intended contract/account.
- Per-order operational data: approved tariff, recipient, address/pickup point and
  package dimensions/weight/items; these cannot be inferred from a token.
- Supply validated provider_payload with tariff_code/packages and provider-required
  recipient/location/sender fields in DeliveryDraft JSON. This is trusted operator
  input, never a public checkout-controlled external payload.
- Offline: `python -m app.scripts.delivery validate FILE.json`; local intent:
  `python -m app.scripts.delivery prepare FILE.json`. Store file outside git.
- After separate external-write approval, `python -m app.scripts.delivery submit FILE.json`.
  Stable idempotency_key becomes CDEK number. Check returned UUID/provider dashboard,
  reference and exactly one order. There is no automatic payment/dispatch confirmation.
- Disable: writes=false; remove credentials/stop submission. Uncertain response:
  reconcile by number before retry, because CDEK number alone is not assumed to
  guarantee provider deduplication. Delivery credentials alone never enable writes.

## Yandex Delivery

- Credential: YANDEX_DELIVERY_TOKEN for the intended business account.
- Prepare DeliveryDraft provider=yandex with checked route_points and items,
  recipient contacts/coordinates and package values under that account's contract.
- Same validate/prepare/submit commands as CDEK. Stable idempotency_key becomes
  request_id. API adapter creates a claim; it does not automatically accept a quote,
  dispatch a courier or take payment. Claim acceptance is an explicit operator step
  in the provider dashboard. This boundary intentionally avoids blind dispatch.
- Smoke after approval: exactly one claim ID, expected route/items, repeated request
  reconciled. Disable writes flag/remove token; cancel a real claim through provider
  procedure, not by deleting local records.

## Monitoring

- Optional credential: SENTRY_DSN. Env MONITORING_ENABLED=true only after activation.
  SDK is pinned/preinstalled; default false never initializes transport.
- Adapter uses no default integrations, locals, request bodies, breadcrumbs or PII;
  before_send strips exception messages and sensitive context. Tracing disabled.
  Configuration reference: https://getsentry.github.io/sentry-python/apidocs.html
- After approval inject a synthetic ERROR in staging, verify delivery without PII,
  configure health/readiness and log/queue alerts from INCIDENT_RUNBOOK.md.
- Disable MONITORING_ENABLED=false/remove DSN/redeploy. No external telemetry sent
  during internal acceptance; adapter validated with fake initialization/transport.
# Buying integration checkpoint

Buying API and XLSX configuration: [BUYING_API.md](BUYING_API.md) and
[BUYING_PRICE_LISTS.md](BUYING_PRICE_LISTS.md).

`EXTERNAL_WRITES_ENABLED=false` and `SUPPLIER_EMAIL_SEND_ENABLED=false` remain
defaults. Live supplier Gmail and MoySklad procurement adapters currently fail closed
even if flags are set; only explicit fake operations are implemented and tested.
The existing customer Gmail readonly token/scope is unchanged.

Future real supplier Gmail sending needs separately authorized credentials/token with
`https://www.googleapis.com/auth/gmail.send` in addition to readonly access needed to
read replies. Do not overwrite `GMAIL_TOKEN_FILE`, reuse its refresh token with an
expanded scope, or automatically invoke OAuth. Provision a separate supplier token
(future `SUPPLIER_GMAIL_TOKEN_FILE`) through explicit consent, bind the sender account,
and implement durable sending/unknown/reconciled states before enabling live sends.
A deterministic Message-ID alone does not guarantee exactly-once Gmail delivery.
After a send timeout, reconcile remotely before retrying; never blindly resend.

`SUPPLIER_REPLIES_ENABLED=true` enables readonly polling of linked real Gmail thread
IDs in the existing email worker. Fake threads are excluded; customer allowed-message
selectors and mass-intake activation remain unchanged. Every matching sender reply
is saved without attempting supplier confirmation semantics. Received HTML is converted
to text; attachment metadata only is retained. Unique Gmail message IDs deduplicate.

Future MoySklad adapter must resolve configured group/warehouse “Внешние поставщики”,
fail actionably if absent, reconcile local purchase IDs with external IDs before retry,
and perform no calls while external writes are disabled. Fake adapter yields stable
counterparty/Supplier Order/Receipt IDs; it never creates a real warehouse or document.
There is no claim of live supplier email or procurement-write readiness yet.

## Current readonly Gmail blocker (2026-09-23)

Redeploy exposed `RefreshError / invalid_grant`: the existing local and staging
refresh tokens match and both are expired/revoked. This was reproduced without
editing credentials, scope or mailbox messages. Backend/manager are independent.

Mailbox owner recovery: preserve the existing configured token file as a private
backup, then perform a deliberate fresh authorization with the existing readonly
OAuth client (`.\.venv\Scripts\python.exe -m app.scripts.gmail_oauth`). The old
invalid token must not be used as the input to this first-authorization run, because
refresh fails before interactive fallback. Transfer the newly authorized readonly
token to the **staging email service only** using the secret-variable procedure above,
then redeploy and verify readiness. Never print token/base64 contents. This is
reauthorization of the existing readonly integration, not supplier send consent.
