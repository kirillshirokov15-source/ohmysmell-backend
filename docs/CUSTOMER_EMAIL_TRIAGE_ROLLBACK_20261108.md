# Customer Gmail triage – rollout and rollback

Scope: only **production manager bot** and **customer Gmail worker**. No database
schema migrations, stock writes, payment changes, OAuth or Telegram allowlist changes.

## Behavior when `OMS_EMAIL_TRIAGE_ENABLED=true` on both services

- **Order** and **possible_order** email: continues into Telegram order feed. A
  missing item/quantity never silently converts a possible order into a question.
- **Inquiry** (`contact_details.mail_intent=inquiry`): preserved in CRM; no
  message is sent to the order group. Managers find it via
  `/emailquestions` or **Ещё → 💬 Вопросы с почты**, with original text.
- **System** email: conservative match of a real `mailer-daemon` /
  `postmaster` sender **and** a mail-delivery failure subject. Preserved as
  inbound + draft for dedupe/audit, but no Telegram order notification.
  Existing delivery reports with old `inquiry` status are excluded from
  both inboxes without DB mutation. Accessible by ID or `/emailtechnical`.
- **Orders**: `/emailorders` lists only orders and possible orders when
  feature flag is enabled. Generic **Заявки** excludes ordinary email
  questions and automated delivery reports. Nonemail sources are unchanged.
- Retry queue is resolved for messages routed to inbox/archive so old
  pending notifications do not later flood Telegram.
- **Historical already-sent Telegram messages stay intact**. No mass deletes.

## Rollback flags

Railway → `eloquent-wisdom` → production:
- `ohmysmell-manager-bot-production` → Variables:
  `OMS_EMAIL_TRIAGE_ENABLED=false` → deploy. Restores former menu and lists.
- `ohmysmell-customer-email-worker-production` → Variables:
  `OMS_EMAIL_TRIAGE_ENABLED=false` → deploy. Restores notifications for
  **future** incoming questions and technical reports; no stored data removed.

For full source rollback, redeploy last-good Railway manager deployment or
`rollback/manager-mail-triage-20261108` pinned to
`da389a8af52243cddbae6f9dfea997e61abc02e0`.
Last-good Gmail worker:
`rollback/gmail-mail-triage-20261108` pinned to
`e2a34219860850f633d071bdf2ab04897ea33f21`.
Do not change other services.

## Verification

- Offline staging: `MANAGER_EMAIL_TRIAGE_SMOKE_OK` and
  `GMAIL_TRIAGE_SMOKE_OK`. Tests cover orders vs questions vs delivery
  reports, legitimate sender with same subject, menu, archive and rollback.
- After deployment confirm `worker_ready` (manager),
  `worker_ownership_acquired` and `email_poll_completed` (Gmail).
- Live synthetic messages are **not** sent by the test; no fake orders.
- Old questions are still in the CRM, even when not in the main group.
