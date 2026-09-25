# Readonly Gmail recovery

Current staging token returns `invalid_grant`; mailbox-owner reauthorization is
required. The integration still requests exactly `gmail.readonly`. Do not grant
send/modify scopes, change the controlled intake selector, or enable mass intake.

1. Set `GMAIL_CREDENTIALS_FILE` and `GMAIL_TOKEN_FILE` to private absolute files
   **outside the repository**. Use the existing OAuth desktop client; keep a private
   backup of the old token. Never paste credentials/token JSON into issues or logs.
2. Deliberately run the hidden local browser consent flow:

   ```powershell
   .\.venv\Scripts\python.exe -m app.scripts.gmail_oauth
   .\.venv\Scripts\python.exe -m app.scripts.gmail_smoke_test
   ```

   The helper handles an expired/revoked refresh token only in this explicit
   interactive mode. The old file is replaced atomically only after success.
   Runtime never opens a browser or widens scopes. A scope mismatch fails closed.
3. After successful readonly smoke, update the **staging email worker only**:

   ```powershell
   $env:PYTHONPATH='.'
   .\.venv\Scripts\python.exe scripts/update_staging_gmail_token.py
   railway redeploy --project 142df0b6-cee3-41a8-9353-ea14ec15f0b0 --environment staging --service ohmysmell-email-worker-staging --yes
   ```

   The script validates staging and readonly scopes, rechecks Gmail read access,
   and passes `GMAIL_TOKEN_JSON_BASE64` to Railway through stdin with captured
   output. It never prints tokens/base64. Runtime `GMAIL_TOKEN_FILE` remains a
   private `/tmp/...` file, reconstructed from the secret variable on redeploy.
   Existing files on a persistent volume are deliberately preserved; if using a
   volume instead of `/tmp`, an operator must explicitly replace that old token
   before restart. The helper refuses this case rather than silently reuse it.
4. Check email `/ready` (service internal endpoint) and manager-only
   `GET /buying/settings/status`. Ready requires a successful poll, not merely a
   running process. Preserve the database cursor and all message IDs.

## Runtime state

The worker starts its health server before building the Gmail provider. On
`invalid_grant`, `/health` remains live and `/ready` returns 503 with
`status=reauth_required`, `action=run_gmail_oauth_then_update_worker_token`, and
`retry_after_seconds=900`. Repeated same-category failures do not produce repeated
traceback/log loops. Transient failures back off exponentially up to 15 minutes.

Other codes: `bad_credentials`, `network_unavailable`, `database_unavailable`,
`worker_error`. Worker DB ownership loss terminates the worker safely. The manager
Settings endpoint reads a sanitized DB heartbeat and returns `worker_stale` after
the heartbeat expires; no credentials or raw provider errors are exposed.
`configured` for MoySklad/Telegram is configuration status, not a live connectivity
claim. `cached` FX includes its source/date and must be displayed as a reference.

No supplier email, mailbox write, cursor reset or production deploy is part of recovery.
