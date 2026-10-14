# Buying Supplier Gmail — prepared, NOT connected

Independent supplier procurement mailbox. Customer wholesale inbound mailbox is unrelated.

## Implemented code paths (not activated)

1. Buying internal Manager creates a supplier, imports or reviews XLSX/CSV price list and offer mapping. No one sends letters merely by importing.
2. Manager groups checkout lines per supplier and confirms purchase snapshot. Only product name and quantity enter the outgoing email. No client details or sale prices.
3. GmailSupplierSender uses SUPPLIER Gmail token and deterministic Message-ID. ExternalOperation journal blocks automatic resend after uncertain result.
4. SupplierReplyPoller accepts replies only in the original supplier thread, with supplier sender address and exact supplier mailbox binding; unmatched messages are ignored.
5. SupplierPriceImport checks sender domain authentication and maps price list rows for review. No automatic external inventory change.
6. Buying Manager/Picker RBAC is enforced server-side; Picker does not see supplier prices.

## Railway environment preparation

Service: `ohmysmell-supplier-email-worker-production`. Start command until OAuth connected: `python -m app.workers.supplier_email_dormant`. It responds on `/health`, does NOT call Gmail, does NOT send emails, and does NOT create MySklad docs.

Variables to prepare **without token values**:
```
APP_ENV=production
EXTERNAL_WRITES_ENABLED=false
SUPPLIER_EMAIL_SEND_ENABLED=false
SUPPLIER_GMAIL_USER_ID=me
EMAIL_POLL_INTERVAL=60
```
The future supplier OAuth variables are `SUPPLIER_GMAIL_CREDENTIALS_JSON` and `SUPPLIER_GMAIL_TOKEN_JSON`; their files or JSON credentials must be distinct from CUSTOMER_GMAIL_*. Set `SUPPLIER_GMAIL_EXPECTED_EMAIL` and `CUSTOMER_GMAIL_EXPECTED_EMAIL` to the two distinct mailbox addresses before any live activation. Never paste secrets into chat or commit to git.

## Later activation (separate release and sign-off)

- Validate supplier OAuth scopes include `gmail.readonly` and `gmail.send`, account identity and separation from customer Gmail. Test readonly supplier login, no purchases created.
- Test one fake purchase → outgoing email preview (no client data) → supplier reply → Buying purchase status and notifications.
- Test one real supplier with explicit Manager approval; enable sending only after read-back/journal reconciliation and a verified supplier recipient. Sending requires `EXTERNAL_WRITES_ENABLED=true` as well as `SUPPLIER_EMAIL_SEND_ENABLED=true`: **never flip these in production merely to start reading emails**, because EXTERNAL_WRITES_ENABLED also permits unrelated external writes. Prefer a separately scoped outgoing-email permission before actual cutover.
- Replace dormant start command with `python -m app.workers.supplier_email_runtime` only once OAuth, separate DB lock and selective reconciliation have passed.
- Never use the purchasing mailbox for ingesting wholesale orders. Keep Tilda fulfilment Own + X separate from Buying external supplier procurement.

## Caution

`DisabledMoySkladProcurementAdapter` is intentionally fail-closed and no external MySklad supplier receipts should be considered live. Supplier mail is **prepared in code**, not connected, and no user should be told that their suppliers have been contacted.
