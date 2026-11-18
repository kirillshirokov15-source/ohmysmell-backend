# Buying supplier Gmail onboarding (separate from customer Gmail)

Supplier account: beauty.lowcoster@gmail.com
Customer wholesale account: wowhairlover@gmail.com

## One-time consent (Windows, account owner)

Use the pre-existing OAuth Desktop client file, kept outside Git:

    C:\Users\kater\ohmysmell secrets\supplier\credentials.json

Open PowerShell:

```powershell
Set-Location "C:\Users\kater\ohmysmell-backend"
$env:SUPPLIER_GMAIL_CREDENTIALS_FILE = "C:\Users\kater\ohmysmell secrets\supplier\credentials.json"
$env:SUPPLIER_GMAIL_TOKEN_FILE = "C:\Users\kater\ohmysmell secrets\supplier\token.json"
$env:SUPPLIER_GMAIL_USER_ID = "me"
$env:SUPPLIER_GMAIL_EXPECTED_EMAIL = "beauty.lowcoster@gmail.com"
$env:CUSTOMER_GMAIL_EXPECTED_EMAIL = "wowhairlover@gmail.com"
.\.venv\Scripts\python.exe -m app.scripts.supplier_gmail_oauth
.\.venv\Scripts\python.exe -m app.scripts.supplier_gmail_smoke_test
```

The owner grants **gmail.readonly + gmail.send** to the SUPPLIER Google
account. Confirm the smoke test prints `SUPPLIER Gmail: connected`,
the correct account and `Readonly INBOX access: OK`. No live email is sent.

Both the client JSON and the generated token JSON contain sensitive secrets:
**never commit them, paste them into public issues/chats, or put them in
client-side Buying configuration.**

## Backend Railway production secret configuration

Add these as production service variables on `ohmysmell-backend`:

- `SUPPLIER_GMAIL_CREDENTIALS_JSON`: complete JSON from supplier credentials.json
- `SUPPLIER_GMAIL_TOKEN_JSON`: complete JSON from supplier token.json
- `SUPPLIER_GMAIL_EXPECTED_EMAIL=beauty.lowcoster@gmail.com`
- `CUSTOMER_GMAIL_EXPECTED_EMAIL=wowhairlover@gmail.com`
- `SUPPLIER_GMAIL_USER_ID=me`

The backend supplier_sender now materializes these JSON variables into distinct
local files on demand. Do not use `CUSTOMER_GMAIL_TOKEN_JSON` for Buying.

For the independent supplier reply worker, reference the same **supplier**
variables from backend via Railway reference variables or provide the same
supplier-specific secrets securely. Keep the worker start command
`python -m app.workers.supplier_email_dormant` and sending disabled until
OAuth smoke checks pass and both credentials and identity are configured.
Then change start command to `python -m app.workers.supplier_email_runtime`
and activate `EMAIL_PRODUCTION_ACTIVATED=true`, with
`SUPPLIER_EMAIL_SEND_ENABLED=false` until explicitly approved.
Check `/health`, `/ready`, worker logs, and `supplier_gmail` health.

## Sending safeguards

Only after successful OAuth verification, connected Buying suppliers and
an authorized test of purchasing flows should real sending be enabled
in backend with `SUPPLIER_EMAIL_SEND_ENABLED=true` and permitted external
writes. The manager must explicitly send a supplier purchase from Buying.
Never send a test to a real supplier or auto-retry uncertain sends.

## Rollback

Backend source before this patch: `0e75f1d25080b058f7cb97f25aef309670642bcc`
on branch `rollback/buying-supplier-gmail-secret-loader-20261118`.
No schema migrations or changes to the CUSTOMER Gmail worker.

## Verification performed

Isolated staging test `BUYING_SUPPLIER_GMAIL_SECRET_LOADER_SMOKE_OK`.
This proves secret materialization and supplier mailbox separation using
fake credentials only. It **does not** prove Google consent, live Gmail API
access or real supplier email delivery.
