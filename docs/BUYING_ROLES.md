# Buying accounts and roles

`manager`: all Buying catalog, supplier/pricing, XLSX, cart, checkout, purchase,
reply and settings operations. `picker`: authenticated pickup list/detail/history
and the **Забрал** action only. Managers may also use the restricted pickup API.
There is no login role selector. The database account determines permissions.

Picker responses use a separate schema: supplier name, address, phone, pickup
notes, purchase number, product names/quantities, sent/received time and receiving
actor. No commercial purchase serializer is reused. Draft/error/cancelled
purchases are invisible. Pickup notes are shared operational text: managers
must not enter prices, customer information or credentials there.

## Bootstrap or password reset

Use the project virtualenv. No default passwords exist. Password input is hidden
and never accepted in command-line arguments. Minimum 12, maximum 1024 characters.
Only salted scrypt hashes (N=32768, r=8, p=3) are stored in the database.

```powershell
.\.venv\Scripts\python.exe -m app.scripts.create_buying_user --staging --username buying_manager --role manager
.\.venv\Scripts\python.exe -m app.scripts.create_buying_user --staging --username buying_picker --role picker
```

`--staging` validates `STAGING_DATABASE_URL` against the production URL and
requires external writes disabled. It never falls back to production. Without
`--staging`, only development and a loopback database are accepted.
Add `--replace` to reset an existing password/role, reactivate the account and
revoke every session belonging to it. For this sprint, run the CLI locally against
staging; do not paste passwords into Railway command arguments or deployment logs.

The autonomous Windows staging bootstrap is
`$env:PYTHONPATH='.'; .\.venv\Scripts\python.exe scripts/buying_staging_accounts.py`.
It creates `buying_manager` and `buying_picker` with random passwords and stores
recovery material in ignored `.staging-artifacts/buying-accounts.dpapi`, encrypted
with Windows DPAPI for the current OS user. It prints usernames/roles only. It
does not overwrite existing accounts. For human login, choose your own password
using the hidden prompt with `--replace`; this is preferable to printing generated
credentials. The HTTP acceptance script can use DPAPI credentials without display.
After manual password reset, old acceptance credentials intentionally stop working.

## Sessions and audit

`BUYING_SESSION_SECRET` must be at least 32 random characters. Legacy
`BUYING_SHARED_PASSWORD` is ignored and cannot grant access. Old sessions without
a user are rejected. Bearer sessions last eight hours; the database stores keyed
token digests only. Logout revokes the current session. A live DB account/role
check on every request immediately enforces inactive status and changed roles.
Secret rotation invalidates all bearer tokens.

Receiving stores first `received_at`, `received_by_user_id`, role and username
snapshot, plus a Buying audit event `actor=user:<id>`. Replays, including by a
different account, preserve the original actor/time. Old received purchases retain
null actor fields: the migration does not invent historical identities.

## Sites

`BUYING_ALLOWED_ORIGINS=https://ohmysmell-buying.christiankvyatkovsky.chatgpt.site`
is configured only for Buying routes. No wildcard. `CORS_ORIGINS` continues to
govern Tilda/public endpoints. Use bearer Authorization, not browser cookies.
Complete frontend examples and error codes: [BUYING_API.md](BUYING_API.md).

Keep `EXTERNAL_WRITES_ENABLED=false` and `SUPPLIER_EMAIL_SEND_ENABLED=false`.
No role grants permission to bypass these guards.
