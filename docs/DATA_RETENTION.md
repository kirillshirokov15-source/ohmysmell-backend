# Data retention / PII and synthetic data

PostgreSQL stores customer name/type, normalized email/phone/Telegram identities,
raw inbound email text/subject/sender, draft contact details/items, order contact
snapshot/items, client dialogue, audit actor IDs and provider operation payloads.
These support order review, contact, deduplication, fulfillment and reconciliation.
Telegram receives authorized manager cards; Gmail adapter reads messages only.
Backups contain the same PII. No raw payload/phone/email/token belongs in technical
logs; log IDs, action, duration and exception class. Do not enable SQL echo or
provider SDK debug logging. Optional monitoring strips messages/context/locals.

Operational policy (configure schedules after owner approval): expired incomplete
client conversation data may be cleared after 24 h; inactive unsubmitted dialogue
rows and client response cache after 30 days only if replay window is closed;
raw processed email bodies after 90 days when order review no longer needs them.
Keep inbound unique-ID tombstones/cursor and order audit/idempotency keys while
orders can be retried. Deleting dedupe rows blindly can create duplicate orders.
Order/accounting retention is an owner-defined business/legal requirement; do not
automatically delete financial or fulfillment audit. Rehearsal backups 7 days;
production target 7 daily +4 weekly encrypted copies, restricted access.

Customer erasure/anonymization procedure: authorize an exact customer ID; inventory
its identities, orders, inbound/drafts, conversation/response cache and provider
payloads. Preview IDs/counts, take restricted backup, then one reviewed transaction
removes identities and unneeded dialogue and replaces names/contact/email/phone,
raw inbound bodies/subjects/comments with redacted values. Preserve order amounts,
items needed for business records, audit and unique dedupe tombstones. Verify all
linked snapshots, not just customers. External provider deletion is separate.
No live/customer records were anonymized automatically in this task.

## Synthetic fixtures

`python -m scripts.synthetic_data create` creates only a new oms_fixture_UUID schema,
migrates it and seeds a clearly named SYNTHETIC customer. Receipt stores run/schema
and a hash of the DB identity, not its URL. Supply that search_path to test sessions.
`python -m scripts.synthetic_data preview --run UUID` reports the exact owned schema.
`python -m scripts.synthetic_data cleanup --run UUID` checks local manifest, selected
staging identity, UUID schema and in-DB marker before dropping only that isolated
schema. public/arbitrary schemas and production equality are rejected. No broad
DELETE, wildcard schema discovery or automatic cleanup of pre-existing test records.

Public staging E2E fixtures carry unique rc-* external IDs, @example.invalid email
and synthetic payment notes; their explicit receipt records exact draft/order IDs.
Existing unknown staging records are preserved. Inspect their full dependency graph
before any separately reviewed removal; do not infer ownership from a name prefix.
