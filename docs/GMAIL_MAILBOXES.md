# Separate CUSTOMER and SUPPLIER Gmail

There are two accounts and two processes. Never copy a customer token into a
supplier variable. No supplier mail is sent by the customer integration.

| Boundary | CUSTOMER | SUPPLIER |
| --- | --- | --- |
| Purpose | Wholesale intake, quantity parsing, draft/order | Procurement replies attached to Buying purchases |
| Exact scopes | `gmail.readonly` | `gmail.readonly`, `gmail.send` |
| Module | `app.workers.customer_email_runtime` | `app.workers.supplier_email_runtime` |
| State | Existing customer cursor/idempotency | Account-bound purchase thread IDs, reply dedupe |
| Settings key | `customer_gmail` | `supplier_gmail` |

`gmail.modify` is rejected. The customer HTTP transport permits GET only. The
supplier reply/smoke transport also permits GET only, despite its token's send
scope. Wholesale ingestion explicitly rejects the supplier provider; supplier
reply polling and the supplier sender reject the customer provider.

## Local manual OAuth

Use private, distinct files **outside the repository**. Set both
`CUSTOMER_GMAIL_EXPECTED_EMAIL` and `SUPPLIER_GMAIL_EXPECTED_EMAIL` to the actual,
different account addresses. Supplier startup requires both; the authenticated
profile must match the supplier address and differ from the customer address.
Customer verifies its expected address whenever configured. This prevents two
separately authorized tokens for the same account from becoming two integrations.

Set local file paths:

```dotenv
CUSTOMER_GMAIL_CREDENTIALS_FILE=C:/private/customer-oauth-client.json
CUSTOMER_GMAIL_TOKEN_FILE=C:/private/customer-token.json
CUSTOMER_GMAIL_USER_ID=me
CUSTOMER_GMAIL_EXPECTED_EMAIL=<customer account address>
SUPPLIER_GMAIL_CREDENTIALS_FILE=C:/private/supplier-oauth-client.json
SUPPLIER_GMAIL_TOKEN_FILE=C:/private/supplier-token.json
SUPPLIER_GMAIL_USER_ID=me
SUPPLIER_GMAIL_EXPECTED_EMAIL=<different supplier account address>
```

Run these explicitly; automation does not open browser consent:

```powershell
.\.venv\Scripts\python.exe -m app.scripts.customer_gmail_oauth
.\.venv\Scripts\python.exe -m app.scripts.customer_gmail_smoke_test
.\.venv\Scripts\python.exe -m app.scripts.supplier_gmail_oauth
.\.venv\Scripts\python.exe -m app.scripts.supplier_gmail_smoke_test
```

Customer invalid_grant recovery remains supported: explicit interactive auth can
replace its revoked token after successful consent; runtime never invokes OAuth.
Smoke prints verified account identity and readonly INBOX access, never token or
client secrets. Supplier smoke additionally verifies send scope; it never calls
messages.send. Leave `EXTERNAL_WRITES_ENABLED=false` and
`SUPPLIER_EMAIL_SEND_ENABLED=false` for every command and deployment.

Local CLI preserves an existing file instead of replacing freshly recovered OAuth
with stale env JSON. Prefer FILE-only configuration locally; runtime JSON on
Railway is authoritative and materialized separately on process restart.

## Railway secrets and workers

Existing `ohmysmell-email-worker-staging` is **CUSTOMER ONLY**. Its old
`app.workers.email_runtime` entrypoint is a customer-only compatibility alias.
Config prepared: `railway.customer-email.staging.toml`.
`SUPPLIER_REPLIES_ENABLED` no longer enables anything in this process.

A separate staging service is required for supplier replies:
`ohmysmell-supplier-email-worker-staging`, source `feature/sales-core-v2`, config
`railway.supplier-email.staging.toml`, start command
`python -m app.workers.supplier_email_runtime`. The config is prepared; no supplier
service is provisioned by this code change. Create it in **staging only**, with its
own secret variables. Do not duplicate the customer service's entire environment.

Both services need `APP_ENV=staging`, staging `DATABASE_URL`,
`EXTERNAL_WRITES_ENABLED=false`, `SUPPLIER_EMAIL_SEND_ENABLED=false`, and
`EMAIL_POLL_INTERVAL=60`. Railway supplies PORT. Use `/health` for liveness and
`/ready` for functional readiness. Give the supplier service no customer OAuth
secret. Give the customer service no supplier OAuth secret.

Customer service secrets:

```dotenv
CUSTOMER_GMAIL_TOKEN_JSON=<complete authorized customer token JSON>
CUSTOMER_GMAIL_CREDENTIALS_JSON=<customer OAuth client JSON, if needed>
CUSTOMER_GMAIL_USER_ID=me
CUSTOMER_GMAIL_EXPECTED_EMAIL=<customer account address>
SUPPLIER_GMAIL_EXPECTED_EMAIL=<supplier account address; identity metadata only>
```

Supplier service secrets:

```dotenv
SUPPLIER_GMAIL_TOKEN_JSON=<complete authorized supplier token JSON>
SUPPLIER_GMAIL_CREDENTIALS_JSON=<supplier OAuth client JSON, if needed>
SUPPLIER_GMAIL_USER_ID=me
SUPPLIER_GMAIL_EXPECTED_EMAIL=<supplier account address>
CUSTOMER_GMAIL_EXPECTED_EMAIL=<customer account address; identity metadata only>
```

JSON means raw JSON, not a filename or base64. With JSON-only configuration,
runtime creates private files under distinct temp `ohmysmell/customer` and
`ohmysmell/supplier` directories. Explicit `*_FILE` remains supported for mounted
private files. Paths may never resolve to the same token file. Restart the owning
worker after updating a JSON secret. Named `*_JSON_BASE64` is also supported for
migration; legacy `GMAIL_*` keys are aliases **only for CUSTOMER**, never supplier.
The backend needs neither account's credentials: it reads sanitized DB heartbeats.

The existing customer secret can be migrated without changing its contents/scopes:
`scripts/configure_customer_gmail_staging.py` copies it into CUSTOMER keys via stdin.
After manual OAuth, upload one token without putting JSON in command arguments:

```powershell
$env:PYTHONPATH='.'
.\.venv\Scripts\python.exe scripts/update_staging_gmail_token.py --mailbox customer
.\.venv\Scripts\python.exe scripts/update_staging_gmail_token.py --mailbox supplier
```

The helper validates exact scopes, performs readonly smoke and targets only the
named staging worker. It fails if the supplier staging service does not exist.
Redeploy **only that worker** after uploading; do not replace the other mailbox.
Customer controlled `GMAIL_ALLOWED_MESSAGE_IDS` / `GMAIL_ORDER_QUERY` selectors and
the existing query activation guard remain unchanged. Mass intake stays disabled.

## Replies, sending and status

Only SUPPLIER Gmail polls supplier threads. A purchase's internal
`supplier_mailbox_account` must equal the verified supplier address. Future live
outbound delivery must save this field with the returned thread/message IDs.
Existing unbound threads are quarantined, not assumed to belong to the new account.
Fake threads are excluded. Reply ingestion also filters by mailbox account plus
exact thread and sender, and Gmail message IDs remain deduplicated. No semantic
confirmation parsing. Telegram delivery stays in the existing manager notification
worker and consumes durable Buying reply events; no second Telegram poller is added.

Live outbound remains deliberately fail-closed even if flags are accidentally
enabled: the production reconciliation/idempotent sending adapter still needs
implementation. Its provider boundary is SUPPLIER-only. Checkout remains preview
then local draft/fake send; no real email is sent in this task. Future body remains
exactly `Product Name – N шт.` lines without greeting, signature or customer data.

`GET /buying/settings/status` is manager-only and now returns two independent
objects, each with `status` and `checked_at`. Status is `connected`,
`reauth_required`, `not_configured` or `error`. Missing/unconfigured worker is
not_configured; stale heartbeat or provider failure is error. Runtime health keeps
more precise sanitized diagnostic codes. invalid_grant backs off 15 minutes with
no traceback loop. Distinct DB ownership locks and processes ensure failure of
one mailbox cannot terminate the other; no cursor/token state is shared.
