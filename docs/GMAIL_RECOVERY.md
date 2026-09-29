# Gmail recovery: choose the mailbox explicitly

The single-mailbox instructions are superseded by
[GMAIL_MAILBOXES.md](GMAIL_MAILBOXES.md). CUSTOMER and SUPPLIER are different
accounts, token files, scopes and worker processes. Never broaden customer scopes.

Customer recovery (readonly only):

```powershell
.\.venv\Scripts\python.exe -m app.scripts.customer_gmail_oauth
.\.venv\Scripts\python.exe -m app.scripts.customer_gmail_smoke_test
```

Supplier setup/recovery (readonly + send, no modify, no test send):

```powershell
.\.venv\Scripts\python.exe -m app.scripts.supplier_gmail_oauth
.\.venv\Scripts\python.exe -m app.scripts.supplier_gmail_smoke_test
```

Set private FILE paths and distinct EXPECTED_EMAIL identities first. Afterwards
use `scripts/update_staging_gmail_token.py --mailbox customer` or `--mailbox supplier`
to update only the corresponding Railway JSON secret, then redeploy that worker.
All exact variables and staging service configuration are in the linked guide.
