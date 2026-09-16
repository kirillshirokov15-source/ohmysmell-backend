# Production readiness and process configuration

Internal readiness is validated independently of live integrations. This is not
production acceptance: production has not been connected or deployed.

## Process matrix

| Process | Explicit entrypoint | Required secrets | Required configuration |
|---|---|---|---|
| Backend | python -m uvicorn app.main:app --host 0.0.0.0 --port PORT | DATABASE_URL, INTERNAL_API_TOKEN >=32 chars | APP_ENV, exact production CORS_ORIGINS |
| Manager | python -m app.workers.manager_bot | DATABASE_URL, TELEGRAM_BOT_TOKEN | APP_ENV, active manager rows; BOT_PRODUCTION_ACTIVATED in production |
| Client | python -m app.workers.client_bot | DATABASE_URL, CLIENT_TELEGRAM_BOT_TOKEN | APP_ENV; BOT_PRODUCTION_ACTIVATED in production |
| Email | python -m app.workers.email_runtime | DATABASE_URL, authorized Gmail token file | APP_ENV, GMAIL_TOKEN_FILE, GMAIL_USER_ID, interval >=5; EMAIL_PRODUCTION_ACTIVATED in production |

Manager/catalog/email matching additionally need read-only MOYSKLAD_TOKEN and the
approved price/warehouse mapping. Client free-text intake does not need MoySklad.
GMAIL_CREDENTIALS_FILE is needed for initial OAuth, not every worker boot when an
already-authorized token exists. Email token file must be writable for refresh.
Use a restricted volume or the implemented secret-env reconstruction described in
EXTERNAL_INTEGRATIONS.md. GMAIL_TOKEN_JSON_BASE64 and GMAIL_CREDENTIALS_JSON_BASE64
are secrets; GMAIL_ALLOWED_MESSAGE_IDS is a non-secret controlled intake selector,
mandatory for staging. Backend/client/manager never perform Gmail authorization.

Common safe defaults: EXTERNAL_WRITES_ENABLED=false, DEBUG_ENDPOINTS_ENABLED=false,
MONITORING_ENABLED=false, PUBLIC_CHECKOUT_ENABLED=false. Client/email credentials
are not required by other processes. Optional delivery secrets are validated at
submission, never at unrelated backend startup. Explicit external writes require
organization configuration and are forbidden in staging.

Template: deploy/production.env.example. Every blank secret is intentionally empty.
IDs, origins, price-type names, intervals, flags and role/entrypoints are non-secret;
DB URLs, internal/bot/provider/OAuth tokens, OAuth files and Sentry DSN are secrets.
Do not copy the whole template to every service: use only its role's variables.
No default production credentials or dummy live tokens are supplied.

## Railway prepared service configuration

Use the same repository/image and selected environment. `deploy/worker-services.json`
contains deploy fields for client/email; apply the matching object through Railway
service configuration, verify the resulting serviceManifest, not CLI exit code alone.
For noninteractive CLI use the JSON patch on stdin (OPERATIONS.md). Health /health,
one replica, overlap 0, drain 45 s, bounded on-failure restart. Alert on /ready too.
Use the same-environment Postgres private DATABASE_URL reference. Give each bot a
unique token; do not start a second poller. Email is one account/cursor per database.
The manager notification loop belongs only to manager worker, never backend/client.
Client service remains unstarted without its token. Gmail now has local controlled
live acceptance; current staging email deployment evidence is in FINISH_REPORT.md.
Production Gmail activation is still a separate decision. The email service must not
receive the manager Telegram token; notifications are delivered by the manager outbox.

## Activation checklist (future, separately authorized)

1. Verify intended production project/environment/domain and owner access. Create
   production DB and restricted runtime role; migration owner is separate. Restrict
   DB access, ingress rate/connection limits and internal API exposure.
2. Install secrets by role from template; configure exact CORS/domain, manager
   allowlist, catalog/price/warehouse mapping. Keep all writes/optional features off.
3. Perform BACKUP_RESTORE.md rehearsal on a COPY. Record restore evidence and deploy
   artifact SHA. Run alembic upgrade head once using migration role; current/check.
4. Deploy backend; /health + /ready, internal endpoints reject unauthorized access,
   debug routes 404, catalog/checkout contract. Activate public checkout only when ready.
5. Set BOT_PRODUCTION_ACTIVATED=true and start one manager worker; /health and /ready,
   authorized manager can complete local canary workflow. Ensure old pollers stopped.
6. Connect Gmail, Tilda and client channel using EXTERNAL_INTEGRATIONS.md live checks.
   Start email only after EMAIL_PRODUCTION_ACTIVATED=true. Configure monitoring and
   backup schedules, readiness/queue alerts, operator ownership and rollback contact.
7. Review canary and observation period. No automated production activation here.
8. A separate decision enables external writes and one controlled provider canary.
   Rollback flag false first; preserve audit and reconcile external side effects.

Application rollback keeps additive DB schema. Do not downgrade j93 or restore a
snapshot over current production without accounting for subsequent business events.

## Internal acceptance matrix

Manager and client workflows, role separation, race/retry/restart, provider failure
responses, API error schemas/input bounds, native backup/restore, historical/fresh
migrations and safe fixture cleanup are covered. See FINISH_REPORT.md for measured
results and REMOTE_STAGING_ACCEPTANCE.md for deployment evidence. No current core
item is deferred to an unspecified future implementation; live provider contracts,
business account values, credentials/domains and operational activation still need
external acceptance. No software test constitutes a guarantee of future provider uptime.
