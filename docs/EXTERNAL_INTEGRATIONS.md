# External integration connection checklists

No integration below was activated by this readiness task. Production/main untouched.
Keep EXTERNAL_WRITES_ENABLED=false until a separate write activation decision.
Commands run in the intended service environment; never copy the staging DB into
production variables. Application code and entrypoints are already provided.

## Gmail (read-only)

- Credentials: Google OAuth desktop client JSON + authorized user token JSON with
  exactly https://www.googleapis.com/auth/gmail.readonly; owner consent required.
- Env: GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, GMAIL_USER_ID=me,
  GMAIL_INITIAL_QUERY, EMAIL_POLL_INTERVAL=60, DATABASE_URL, APP_ENV;
  MOYSKLAD_TOKEN/price mapping for product matching. Production additionally
  EMAIL_PRODUCTION_ACTIVATED=true after approval. Internal API token/CORS not needed.
- Put client JSON on a restricted persistent volume; run
  `python -m app.scripts.gmail_oauth` once in an interactive owner-controlled session.
  Transfer authorized token to the worker volume without printing it. Runtime may
  refresh/rewrite that file; do not use ephemeral container storage for it.
- Smoke: `python -m app.scripts.gmail_smoke_test`; confirm account privately and no writes.
  Launch `python -m app.workers.email_runtime`, one replica, /health and /ready.
  Send one test order email, verify one draft, restart, verify unchanged draft ID
  and saved cursor. History 404 rescans INBOX including already-read messages; durable IDs prevent duplicates.
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
