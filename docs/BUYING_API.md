# OhMySmell Buying REST contract

Buying is a shared internal workspace with no customer or sales-order references.
Base path: `/buying`. Machine-readable contract: [openapi.json](openapi.json), live
`/openapi.json`. Frontend belongs to Sites and is not implemented in this repository.

## Authentication and browser connection

Configure `BUYING_SHARED_PASSWORD` (at least 12 characters) and
`BUYING_SESSION_SECRET` (at least 32 random characters). Without both, Buying fails
closed with 503. Passwords never appear in API responses or logs.

`POST /buying/auth/login` body `{"password":"<shared password>"}` returns
`{"access_token":"...","token_type":"bearer","expires_in":28800}`.
Every other Buying route requires `Authorization: Bearer <access_token>`.
Store the token in SPA memory, never in URLs. Sessions persist in PostgreSQL,
expire after 8 hours, and can be revoked by `POST /buying/auth/logout`.
Password/secret rotation invalidates all existing tokens. DB stores keyed digests,
not bearer tokens. Login has a per-process global limit of 30 attempts/minute;
use a reverse-proxy limit when scaling beyond this small internal workspace.
Responses use `Cache-Control: no-store`. No cookies/CSRF credentials are used.

Set the exact HTTPS Sites origin in `CORS_ORIGINS`; wildcards are rejected.
Allowed methods include GET, POST, PATCH, DELETE, and Authorization/Idempotency-Key
headers. No per-employee identity is claimed: Buying audit identifies a shared
session digest. Telegram corrections record the real Telegram user ID separately.

## Endpoints

| Method/path after `/buying` | Request / behavior |
| --- | --- |
| GET `/suppliers?offset=0&limit=50` | Configured external suppliers, parser, timestamps, active offer count |
| POST `/suppliers` | `{name,email,currency:"RUB" or "USD",parser:{version:"columns-v1",sheet:1,first_row:2,name_column:"A",price_column:"B",sku_column:null}}` |
| GET `/suppliers/{id}` | Supplier details; no counterparty tokens/details |
| POST `/suppliers/{id}/configure` | Same supplier body; adopts an existing external supplier once |
| POST `/offers/{id}/mapping` | `{name:"Exact supplier product name"}`; adopts an existing SupplierOffer |
| POST `/products/mappings` | `{product_id:"existing-supply-id",name:"Canonical name"}`; explicit known identity before import |
| GET `/catalog?q=marvis&sort=cheapest&offset=0&limit=50` | Canonical product pagination; `sort=cheapest|supplier|newest` |
| GET `/products/{id}/offers` | Active supplier offers for a canonical product |
| GET `/offers/{id}/price-history?offset=0&limit=50` | Old/new minor-unit prices, currency, import ID, timestamp |
| POST `/offers/{id}/estimate` | Refresh read-only FX quote through replaceable provider (currently CBR) |
| POST `/suppliers/{id}/price-lists/preview?filename=list.xlsx` | Raw XLSX binary body, max 2 MiB; not multipart |
| POST `/suppliers/{id}/price-lists/import` | `{import_id:"uuid",mappings:{"ROW_NUMBER":"candidate-product-id"}}`; atomic, replay-safe |
| GET `/cart` | Shared durable cart; current prices and `price_changed_since_added` |
| POST `/cart/items` | `{offer_id:1,quantity:3}`; upsert absolute quantity, repeat-safe |
| PATCH `/cart/items/{offer_id}` | `{quantity:3}`; pieces, integer 1..100000 |
| DELETE `/cart/items/{offer_id}` | Remove; repeat-safe |
| DELETE `/cart` | Clear shared cart |
| POST `/checkout/preview` | Deterministic supplier groups and SHA-256 `fingerprint` |
| POST `/checkout/confirm` | `{fingerprint:"..."}`, header `Idempotency-Key` 8..100 chars |
| GET `/purchases?supplier_id=1&offset=0&limit=50` | Newest first, total count, immutable snapshots |
| GET `/purchases/{id}` | Snapshot, email body, state, replies, simulated external references |
| POST `/purchases/{id}/simulate-send` | Explicit staging/development-only fake email + supplier-order hook |
| POST `/purchases/{id}/received` | Idempotent local transition, audit/group event, fake receipt hook |

List limits are 1..100. Purchase date filtering is not implemented. Price history
is a separate API, not included in catalog payloads. Historical imports do not
automatically deactivate rows absent from a later workbook (incremental import).

## Catalog response

`{products:[{id,name,offers:[...]}],total,offset,limit}`. Every offer includes:
`id,product_id,supplier_id,supplier_name,name,supplier_sku,purchase_price_minor,
currency_code,approximate_rub_minor,approximate,fx_source,fx_rate_date,
fx_rate_to_rub,price_list_updated_at,availability,active,moysklad_match_state`.
All money is integer minor units. FX rates are decimal strings; no floats.
USD without a quote has null RUB estimate and sorts after priced offers.
CBR is a fallback reference rate, not a claim of a live exchange/market rate.
Frontend must display approximate values and quote date explicitly.

Canonical IDs starting `buying-` are local. Existing MoySklad identities must be
registered explicitly to avoid inventing matches. No fuzzy candidate is auto-selected.
All suppliers share the logical warehouse “Внешние поставщики”. Catalog response
models explicitly allowlist procurement fields; sale prices/margins/customer data
are not accessible through this API.

## Checkout semantics

Preview response: `{fingerprint,suppliers:[{supplier_id,supplier_name,recipient,
items,currency,total_minor,approximate_rub_minor,approximate,email_body}]}`.
Each item snapshots offer/product IDs, name, quantity, unit procurement price and
FX rate/source/date. Body consists only of `Product Name – N шт.` lines in stable
offer order. Free-text editing is deliberately not supported in this version.

Confirm creates one draft purchase per supplier, clears the cart in the same
transaction, and returns `{purchase_ids:[...],real_email_sent:false}`. It does not
send an email. Save the idempotency key before submitting and reuse it on timeout.
Same key+fingerprint returns the original IDs even after cart changes. Different
fingerprint with the same key or changed cart/prices returns 409. A new key after
a successful checkout cannot duplicate the now-empty cart.

Explicit `simulate-send` sets `status=sent,send_state=simulated`; message/thread/
counterparty/supplier-order IDs have `fake-` prefixes. Never present this as a real
supplier email. `received` accepts only sent purchases and is repeat-safe.
Confirmed snapshot costs never change after imports or later FX refreshes.

## Replies and future live writes

`SUPPLIER_EMAIL_SEND_ENABLED=false` and `EXTERNAL_WRITES_ENABLED=false` remain
defaults. Live adapters deliberately fail closed even if both flags are enabled:
OAuth send setup and uncertain-outcome reconciliation are not complete. There is
no live email endpoint and no live MoySklad procurement write client in this slice.

`SUPPLIER_REPLIES_ENABLED=false` by default. When explicitly enabled in the existing
email worker, the readonly poller reads only purchase-linked non-fake Gmail threads,
matches the exact supplier sender, and stores any reply regardless of meaning.
Gmail message ID is unique. Archived replies are supported. It does not alter the
customer-intake query or controlled message selector. Safe text and attachment
metadata are returned as text; the frontend must never insert them as HTML.
Synthetic reply ingestion is exercised through the service entry point in tests;
there is no public endpoint that can forge supplier replies.

## Errors

401 invalid/missing/revoked token, 403 unavailable action, 404 unknown entity,
409 stale preview/state/mapping conflict, 413 upload too large, 422 invalid input,
429 login limit, 503 unconfigured auth/provider or temporary backend error.
Production middleware returns safe error envelopes without stack traces/input values.
Do not retry confirm with a new key after a transport error.
