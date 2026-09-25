# OhMySmell Buying: Sites API contract

Staging base URL: `https://ohmysmell-backend-staging-staging.up.railway.app`.
Exact Sites origin: `https://ohmysmell-buying.christiankvyatkovsky.chatgpt.site`.
Set `BUYING_ALLOWED_ORIGINS` to that origin; comma-separated explicit origins are supported.
No wildcard/cookies. Existing `CORS_ORIGINS` continues to govern non-Buying paths.

1. POST login with username/password; there is no role selector or shared-password fallback.
2. Keep access_token in SPA memory; send `Authorization: Bearer <token>` on every other endpoint.
3. GET `/buying/auth/me`: manager opens full Buying UI, picker opens pickup UI.
4. On 401 clear token and show login. On 403 show permission error; do not retry as another role.
5. POST logout revokes current session; clear local token. Expiry: 8 hours. Other sessions remain valid.

Role restrictions are enforced by backend dependencies, including direct requests. Account deactivation
and role changes take effect on the next request. Password reset CLI revokes all user sessions;
session-secret rotation revokes all sessions. Legacy anonymous-user sessions cannot authenticate.
Never put bearer tokens in URLs or store passwords. All Buying responses are non-cacheable.

Money: integer minor units (100 minor = 1 RUB/USD); FX rates: decimal strings, never floats.
USD RUB estimates may be null and are approximate; confirmed purchase snapshots are immutable.
No customer, margin, retail sale price or customer-order fields exist in this workspace.
All date/time values are ISO 8601 UTC; render in the user's local timezone.
Synthetic examples below do not represent live suppliers or credentials.

Machine-readable schemas: [openapi.json](openapi.json), also `/openapi.json` on staging.
Account setup: [BUYING_ROLES.md](BUYING_ROLES.md). XLSX layouts: [BUYING_PRICE_LISTS.md](BUYING_PRICE_LISTS.md).

## Errors and retries

All successful calls below return HTTP 200. All protected calls can return 401 (missing/expired/
revoked/inactive session), 403 (role), 422 (validation) or 503 (configuration/backend unavailable).
Entity/state-specific errors are listed per endpoint. 413 is possible for oversized bodies.
Example error: `{"detail":"Manager role required"}`. Validation envelope:
`{"detail":{"code":"validation_error","fields":[{"loc":["body","username"],"type":"missing"}]}}`.
Unexpected failures return a safe service_unavailable envelope, never provider details/stack traces.
The frontend must accept detail as either a string or an object.

Checkout uses an Idempotency-Key. Never generate a new key just because the response timed out.
Received and import replay are safe. Do not automatically retry supplier creation after an unknown outcome.
Preflight OPTIONS requires no token; unlisted browser origins receive no allow-origin header.
CORS is a browser policy; authorization is required even without Origin.

Real supplier email and MoySklad writes remain disabled. No live send endpoint exists.
Draft purchases are not pickup tasks until the explicit staging simulation marks them sent.
Readonly supplier reply polling is optional and requires working Gmail OAuth; no semantic confirmation parsing.


## POST /buying/auth/login

Auth: None. Allowed role: anonymous.

Request JSON:
```json
{
  "username": "buying_manager",
  "password": "<entered password>"
}
```

Response JSON (200):
```json
{
  "access_token": "<opaque token>",
  "token_type": "bearer",
  "expires_in": 28800
}
```

Error status codes: 401, 422, 429, 503. No role field. Username is case-insensitive ASCII; password whitespace is significant. Limit: 30 attempts/minute/process; Retry-After: 60.


## GET /buying/auth/me

Auth: Bearer session. Allowed role: manager or picker.

Request: none (no body).

Response JSON (200):
```json
{
  "id": 1,
  "username": "buying_manager",
  "role": "manager"
}
```

Error status codes: 401, 403, 422, 503.


## POST /buying/auth/logout

Auth: Bearer session. Allowed role: manager or picker.

Request: none (no body).

Response JSON (200):
```json
{
  "revoked": true
}
```

Error status codes: 401, 403, 422, 503.


## GET /buying/catalog

Auth: Bearer session. Allowed role: manager.

Request: none (no body).

Response JSON (200):
```json
{
  "products": [
    {
      "id": "buying-00000000-0000-0000-0000-000000000001",
      "name": "Synthetic perfume 100 ml",
      "offers": [
        {
          "id": 1,
          "product_id": "buying-00000000-0000-0000-0000-000000000001",
          "supplier_id": 1,
          "supplier_name": "Synthetic supplier",
          "name": "Synthetic perfume 100 ml",
          "supplier_sku": null,
          "purchase_price_minor": 1250,
          "currency_code": "RUB",
          "approximate_rub_minor": 1250,
          "approximate": false,
          "fx_source": null,
          "fx_rate_date": null,
          "fx_rate_to_rub": null,
          "price_list_updated_at": "2026-09-25T12:00:00Z",
          "availability": "on_request",
          "active": true,
          "moysklad_match_state": "local"
        }
      ]
    }
  ],
  "total": 1,
  "offset": 0,
  "limit": 50
}
```

Error status codes: 401, 403, 422, 503. Query: q (partial name, max 200), sort=cheapest|supplier|newest, offset>=0, limit=1..100 (default 50). Canonical-product pagination. No automatic fuzzy selection.


## GET /buying/products/{product_id}/offers

Auth: Bearer session. Allowed role: manager.

Request: none (no body).

Response JSON (200):
```json
{
  "offers": [
    {
      "id": 1,
      "product_id": "buying-00000000-0000-0000-0000-000000000001",
      "supplier_id": 1,
      "supplier_name": "Synthetic supplier",
      "name": "Synthetic perfume 100 ml",
      "supplier_sku": null,
      "purchase_price_minor": 1250,
      "currency_code": "RUB",
      "approximate_rub_minor": 1250,
      "approximate": false,
      "fx_source": null,
      "fx_rate_date": null,
      "fx_rate_to_rub": null,
      "price_list_updated_at": "2026-09-25T12:00:00Z",
      "availability": "on_request",
      "active": true,
      "moysklad_match_state": "local"
    }
  ]
}
```

Error status codes: 401, 403, 422, 503. Path: canonical product_id. Unknown product returns an empty offers list.


## GET /buying/offers/{offer_id}/price-history

Auth: Bearer session. Allowed role: manager.

Request: none (no body).

Response JSON (200):
```json
{
  "history": [
    {
      "id": 1,
      "offer_id": 1,
      "old_price_minor": 1000,
      "new_price_minor": 1250,
      "currency": "RUB",
      "import_id": "00000000-0000-0000-0000-000000000002",
      "changed_at": "2026-09-25T12:00:00Z"
    }
  ]
}
```

Error status codes: 401, 403, 422, 503, 404. Query: offset>=0, limit=1..100.


## POST /buying/offers/{offer_id}/estimate

Auth: Bearer session. Allowed role: manager.

Request: none (no body).

Response JSON (200):
```json
{
  "offer_id": 1,
  "fx_rate_to_rub": "90.0000000000",
  "fx_source": "cbr",
  "fx_rate_date": "2026-09-25"
}
```

Error status codes: 401, 403, 422, 503, 404. Refreshes only a read-only FX quote. CBR is a reference/fallback rate, not a live bank selling rate.


## GET /buying/cart

Auth: Bearer session. Allowed role: manager.

Request: none (no body).

Response JSON (200):
```json
{
  "items": [
    {
      "offer": {
        "id": 1,
        "product_id": "buying-00000000-0000-0000-0000-000000000001",
        "supplier_id": 1,
        "supplier_name": "Synthetic supplier",
        "name": "Synthetic perfume 100 ml",
        "supplier_sku": null,
        "purchase_price_minor": 1250,
        "currency_code": "RUB",
        "approximate_rub_minor": 1250,
        "approximate": false,
        "fx_source": null,
        "fx_rate_date": null,
        "fx_rate_to_rub": null,
        "price_list_updated_at": "2026-09-25T12:00:00Z",
        "availability": "on_request",
        "active": true,
        "moysklad_match_state": "local"
      },
      "quantity": 2,
      "price_changed_since_added": false,
      "created_at": "2026-09-25T12:00:00Z",
      "updated_at": "2026-09-25T12:00:00Z"
    }
  ]
}
```

Error status codes: 401, 403, 422, 503.


## POST /buying/cart/items

Auth: Bearer session. Allowed role: manager.

Request JSON:
```json
{
  "offer_id": 1,
  "quantity": 2
}
```

Response JSON (200):
```json
{
  "offer_id": 1,
  "quantity": 2
}
```

Error status codes: 401, 403, 422, 503, 404. Absolute quantity upsert, not increment. Strict integer 1..100000 pieces. Shared persistent cart.


## PATCH /buying/cart/items/{offer_id}

Auth: Bearer session. Allowed role: manager.

Request JSON:
```json
{
  "quantity": 3
}
```

Response JSON (200):
```json
{
  "offer_id": 1,
  "quantity": 3
}
```

Error status codes: 401, 403, 422, 503, 404.


## DELETE /buying/cart/items/{offer_id}

Auth: Bearer session. Allowed role: manager.

Request: none (no body).

Response JSON (200):
```json
{
  "removed": true
}
```

Error status codes: 401, 403, 422, 503. Repeat-safe, including absent item.


## DELETE /buying/cart

Auth: Bearer session. Allowed role: manager.

Request: none (no body).

Response JSON (200):
```json
{
  "cleared": true
}
```

Error status codes: 401, 403, 422, 503.


## POST /buying/checkout/preview

Auth: Bearer session. Allowed role: manager.

Request: none (no body).

Response JSON (200):
```json
{
  "fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "suppliers": [
    {
      "supplier_id": 1,
      "supplier_name": "Synthetic supplier",
      "recipient": "supplier@example.invalid",
      "items": [
        {
          "offer_id": 1,
          "product_id": "buying-00000000-0000-0000-0000-000000000001",
          "name": "Synthetic perfume 100 ml",
          "quantity": 2,
          "unit_price_minor": 1250,
          "approximate_rub_minor": 1250,
          "fx_rate_to_rub": null,
          "fx_source": null,
          "fx_rate_date": null
        }
      ],
      "currency": "RUB",
      "total_minor": 2500,
      "approximate_rub_minor": 2500,
      "approximate": false,
      "email_body": "Synthetic perfume 100 ml – 2 шт."
    }
  ]
}
```

Error status codes: 401, 403, 422, 503, 409. Empty cart, inactive/unavailable offers or missing supplier email: 409. Body contains product/quantity lines only; editing is unsupported.


## POST /buying/checkout/confirm

Auth: Bearer session. Allowed role: manager.

Request JSON:
```json
{
  "fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

Response JSON (200):
```json
{
  "purchase_ids": [
    1
  ],
  "real_email_sent": false
}
```

Error status codes: 401, 403, 422, 503, 409. Required header Idempotency-Key: 8..100 characters. Save key BEFORE submitting; reuse same key and fingerprint after timeout. Stale cart/prices or conflicting key: 409. One draft per supplier; clears cart atomically. Does not send email.


## GET /buying/purchases

Auth: Bearer session. Allowed role: manager.

Request: none (no body).

Response JSON (200):
```json
{
  "purchases": [
    {
      "supplier_id": 1,
      "supplier_name": "Synthetic supplier",
      "recipient": "supplier@example.invalid",
      "items": [
        {
          "offer_id": 1,
          "product_id": "buying-00000000-0000-0000-0000-000000000001",
          "name": "Synthetic perfume 100 ml",
          "quantity": 2,
          "unit_price_minor": 1250,
          "approximate_rub_minor": 1250,
          "fx_rate_to_rub": null,
          "fx_source": null,
          "fx_rate_date": null
        }
      ],
      "currency": "RUB",
      "total_minor": 2500,
      "approximate_rub_minor": 2500,
      "approximate": false,
      "email_body": "Synthetic perfume 100 ml – 2 шт.",
      "id": 1,
      "number": "B-000001",
      "item_count": 1,
      "status": "sent",
      "send_state": "simulated",
      "created_at": "2026-09-25T12:00:00Z",
      "updated_at": "2026-09-25T12:00:00Z",
      "sent_at": "2026-09-25T12:00:00Z",
      "received_at": null,
      "received_by_user_id": null,
      "received_by_role": null,
      "received_by_username": null,
      "message_id": "fake-message:1",
      "thread_id": "fake-thread:1",
      "external_ids": {
        "supplier_order": "fake-order:1",
        "counterparty": "fake-supplier:1"
      }
    }
  ],
  "total": 1,
  "offset": 0,
  "limit": 50
}
```

Error status codes: 401, 403, 422, 503. Query: supplier_id optional, offset>=0, limit=1..100. Newest first. No date filter yet.


## GET /buying/purchases/{purchase_id}

Auth: Bearer session. Allowed role: manager.

Request: none (no body).

Response JSON (200):
```json
{
  "supplier_id": 1,
  "supplier_name": "Synthetic supplier",
  "recipient": "supplier@example.invalid",
  "items": [
    {
      "offer_id": 1,
      "product_id": "buying-00000000-0000-0000-0000-000000000001",
      "name": "Synthetic perfume 100 ml",
      "quantity": 2,
      "unit_price_minor": 1250,
      "approximate_rub_minor": 1250,
      "fx_rate_to_rub": null,
      "fx_source": null,
      "fx_rate_date": null
    }
  ],
  "currency": "RUB",
  "total_minor": 2500,
  "approximate_rub_minor": 2500,
  "approximate": false,
  "email_body": "Synthetic perfume 100 ml – 2 шт.",
  "id": 1,
  "number": "B-000001",
  "item_count": 1,
  "status": "sent",
  "send_state": "simulated",
  "created_at": "2026-09-25T12:00:00Z",
  "updated_at": "2026-09-25T12:00:00Z",
  "sent_at": "2026-09-25T12:00:00Z",
  "received_at": null,
  "received_by_user_id": null,
  "received_by_role": null,
  "received_by_username": null,
  "message_id": "fake-message:1",
  "thread_id": "fake-thread:1",
  "external_ids": {
    "supplier_order": "fake-order:1",
    "counterparty": "fake-supplier:1"
  },
  "replies": [
    {
      "message_id": "synthetic-reply",
      "thread_id": "fake-thread-1",
      "received_at": "2026-09-25T12:00:00Z",
      "subject": "Re: order",
      "body": "Supplier text",
      "attachments": []
    }
  ]
}
```

Error status codes: 401, 403, 422, 503, 404. All reply bodies/subjects/attachment filenames are untrusted text. Render as text, never raw HTML.


## POST /buying/purchases/{purchase_id}/received

Auth: Bearer session. Allowed role: manager.

Request: none (no body).

Response JSON (200):
```json
{
  "supplier_id": 1,
  "supplier_name": "Synthetic supplier",
  "recipient": "supplier@example.invalid",
  "items": [
    {
      "offer_id": 1,
      "product_id": "buying-00000000-0000-0000-0000-000000000001",
      "name": "Synthetic perfume 100 ml",
      "quantity": 2,
      "unit_price_minor": 1250,
      "approximate_rub_minor": 1250,
      "fx_rate_to_rub": null,
      "fx_source": null,
      "fx_rate_date": null
    }
  ],
  "currency": "RUB",
  "total_minor": 2500,
  "approximate_rub_minor": 2500,
  "approximate": false,
  "email_body": "Synthetic perfume 100 ml – 2 шт.",
  "id": 1,
  "number": "B-000001",
  "item_count": 1,
  "status": "received",
  "send_state": "simulated",
  "created_at": "2026-09-25T12:00:00Z",
  "updated_at": "2026-09-25T12:00:00Z",
  "sent_at": "2026-09-25T12:00:00Z",
  "received_at": "2026-09-25T12:00:00Z",
  "received_by_user_id": 1,
  "received_by_role": "manager",
  "received_by_username": "buying_manager",
  "message_id": "fake-message:1",
  "thread_id": "fake-thread:1",
  "external_ids": {
    "supplier_order": "fake-order:1",
    "counterparty": "fake-supplier:1"
  }
}
```

Error status codes: 401, 403, 422, 503, 404, 409. Only sent purchases; replay returns original received time/actor, no new audit event. Fake receipt only.


## POST /buying/purchases/{purchase_id}/simulate-send

Auth: Bearer session. Allowed role: manager.

Request: none (no body).

Response JSON (200):
```json
{
  "supplier_id": 1,
  "supplier_name": "Synthetic supplier",
  "recipient": "supplier@example.invalid",
  "items": [
    {
      "offer_id": 1,
      "product_id": "buying-00000000-0000-0000-0000-000000000001",
      "name": "Synthetic perfume 100 ml",
      "quantity": 2,
      "unit_price_minor": 1250,
      "approximate_rub_minor": 1250,
      "fx_rate_to_rub": null,
      "fx_source": null,
      "fx_rate_date": null
    }
  ],
  "currency": "RUB",
  "total_minor": 2500,
  "approximate_rub_minor": 2500,
  "approximate": false,
  "email_body": "Synthetic perfume 100 ml – 2 шт.",
  "id": 1,
  "number": "B-000001",
  "item_count": 1,
  "status": "sent",
  "send_state": "simulated",
  "created_at": "2026-09-25T12:00:00Z",
  "updated_at": "2026-09-25T12:00:00Z",
  "sent_at": "2026-09-25T12:00:00Z",
  "received_at": null,
  "received_by_user_id": null,
  "received_by_role": null,
  "received_by_username": null,
  "message_id": "fake-message:1",
  "thread_id": "fake-thread:1",
  "external_ids": {
    "supplier_order": "fake-order:1",
    "counterparty": "fake-supplier:1"
  }
}
```

Error status codes: 401, 403, 422, 503, 404, 409. Staging/development only. Fake supplier email and Supplier Order hook. Never label this a real email.


## GET /buying/suppliers

Auth: Bearer session. Allowed role: manager.

Request: none (no body).

Response JSON (200):
```json
{
  "suppliers": [
    {
      "id": 1,
      "name": "Synthetic supplier",
      "email": "supplier@example.invalid",
      "currency": "RUB",
      "parser": {
        "version": "columns-v1",
        "sheet": 1,
        "first_row": 2,
        "name_column": "A",
        "price_column": "B",
        "sku_column": null
      },
      "status": "active",
      "latest_price_list_upload": "2026-09-25T12:00:00Z",
      "active_offer_count": 1,
      "created_at": "2026-09-25T12:00:00Z",
      "updated_at": "2026-09-25T12:00:00Z",
      "pickup_address": "Test street 1",
      "phone": "+70000000000",
      "pickup_notes": "Call on arrival"
    }
  ],
  "offset": 0,
  "limit": 50
}
```

Error status codes: 401, 403, 422, 503. Query: offset>=0, limit=1..100. One logical warehouse: Внешние поставщики.


## POST /buying/suppliers

Auth: Bearer session. Allowed role: manager.

Request JSON:
```json
{
  "name": "Synthetic supplier",
  "email": "supplier@example.invalid",
  "currency": "RUB",
  "parser": {
    "version": "columns-v1",
    "sheet": 1,
    "first_row": 2,
    "name_column": "A",
    "price_column": "B",
    "sku_column": null
  }
}
```

Response JSON (200):
```json
{
  "id": 1
}
```

Error status codes: 401, 403, 422, 503.


## GET /buying/suppliers/{supplier_id}

Auth: Bearer session. Allowed role: manager.

Request: none (no body).

Response JSON (200):
```json
{
  "id": 1,
  "name": "Synthetic supplier",
  "email": "supplier@example.invalid",
  "currency": "RUB",
  "parser": {
    "version": "columns-v1",
    "sheet": 1,
    "first_row": 2,
    "name_column": "A",
    "price_column": "B",
    "sku_column": null
  },
  "status": "active",
  "latest_price_list_upload": "2026-09-25T12:00:00Z",
  "active_offer_count": 1,
  "created_at": "2026-09-25T12:00:00Z",
  "updated_at": "2026-09-25T12:00:00Z",
  "pickup_address": "Test street 1",
  "phone": "+70000000000",
  "pickup_notes": "Call on arrival"
}
```

Error status codes: 401, 403, 422, 503, 404.


## PATCH /buying/suppliers/{supplier_id}/pickup

Auth: Bearer session. Allowed role: manager.

Request JSON:
```json
{
  "pickup_address": "Test street 1",
  "phone": "+70000000000",
  "pickup_notes": "Call on arrival"
}
```

Response JSON (200):
```json
{
  "id": 1,
  "name": "Synthetic supplier",
  "email": "supplier@example.invalid",
  "currency": "RUB",
  "parser": {
    "version": "columns-v1",
    "sheet": 1,
    "first_row": 2,
    "name_column": "A",
    "price_column": "B",
    "sku_column": null
  },
  "status": "active",
  "latest_price_list_upload": "2026-09-25T12:00:00Z",
  "active_offer_count": 1,
  "created_at": "2026-09-25T12:00:00Z",
  "updated_at": "2026-09-25T12:00:00Z",
  "pickup_address": "Test street 1",
  "phone": "+70000000000",
  "pickup_notes": "Call on arrival"
}
```

Error status codes: 401, 403, 422, 503, 404. Partial update: omitted values preserved; null clears a field. Limits: address 1000, phone 80, notes 2000 characters. Keep pickup notes free of commercial/customer data; pickers read them.


## POST /buying/suppliers/{supplier_id}/configure

Auth: Bearer session. Allowed role: manager.

Request JSON:
```json
{
  "name": "Synthetic supplier",
  "email": "supplier@example.invalid",
  "currency": "RUB",
  "parser": {
    "version": "columns-v1",
    "sheet": 1,
    "first_row": 2,
    "name_column": "A",
    "price_column": "B",
    "sku_column": null
  }
}
```

Response JSON (200):
```json
{
  "id": 1
}
```

Error status codes: 401, 403, 422, 503, 404, 409. One-time adoption of an existing external supplier. Not a general update endpoint. Already configured/currency conflict: 409.


## POST /buying/offers/{offer_id}/mapping

Auth: Bearer session. Allowed role: manager.

Request JSON:
```json
{
  "name": "Synthetic perfume 100 ml"
}
```

Response JSON (200):
```json
{
  "offer_id": 1,
  "product_id": "buying-00000000-0000-0000-0000-000000000001"
}
```

Error status codes: 401, 403, 422, 503, 404, 409. Adopt an existing supplier offer without duplicating its identity.


## POST /buying/products/mappings

Auth: Bearer session. Allowed role: manager.

Request JSON:
```json
{
  "product_id": "buying-00000000-0000-0000-0000-000000000001",
  "name": "Synthetic perfume 100 ml"
}
```

Response JSON (200):
```json
{
  "product_id": "buying-00000000-0000-0000-0000-000000000001"
}
```

Error status codes: 401, 403, 422, 503, 404, 409. Register an existing local supply identity before XLSX matching.


## POST /buying/suppliers/{supplier_id}/price-lists/preview

Auth: Bearer session. Allowed role: manager.

Request: raw XLSX bytes (no JSON).

Response JSON (200):
```json
{
  "import_id": "00000000-0000-0000-0000-000000000002",
  "counts": {
    "total": 1,
    "valid": 1,
    "invalid": 0,
    "new": 1,
    "changed": 0,
    "unchanged": 0,
    "ambiguous": 0
  },
  "rows": [
    {
      "row": 2,
      "name": "Synthetic perfume 100 ml",
      "sku": null,
      "mapping_key": "name:synthetic perfume 100 ml",
      "price_minor": 1250,
      "error": null,
      "offer_id": null,
      "old_price_minor": null,
      "product_id": null,
      "candidates": [],
      "state": "new"
    }
  ],
  "currency": "RUB",
  "parser_version": "columns-v1"
}
```

Error status codes: 401, 403, 422, 503, 404, 413. Required query filename=synthetic.xlsx. Raw binary body, NOT multipart/JSON. Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet. Max 2 MiB, 5000 rows; macros/formulas not executed. Supplier-specific parser settings are used.


## POST /buying/suppliers/{supplier_id}/price-lists/import

Auth: Bearer session. Allowed role: manager.

Request JSON:
```json
{
  "import_id": "00000000-0000-0000-0000-000000000002",
  "mappings": {}
}
```

Response JSON (200):
```json
{
  "import_id": "00000000-0000-0000-0000-000000000002",
  "imported_at": "2026-09-25T12:00:00Z",
  "counts": {
    "total": 1,
    "valid": 1,
    "invalid": 0,
    "new": 1,
    "changed": 0,
    "unchanged": 0,
    "ambiguous": 0
  }
}
```

Error status codes: 401, 403, 422, 503, 404, 409. For ambiguous rows supply mappings={"2":"listed-candidate-id"}. Invalid rows require corrected upload. Atomic, repeat-safe import; stale preview conflicts. Missing rows do not deactivate prior offers.


## GET /buying/settings/status

Auth: Bearer session. Allowed role: manager.

Request: none (no body).

Response JSON (200):
```json
{
  "gmail": {
    "status": "reauth_required",
    "checked_at": "2026-09-25T12:00:00Z",
    "reauth_required": true
  },
  "moysklad": {
    "status": "configured",
    "writes_enabled": false
  },
  "telegram": {
    "status": "not_configured"
  },
  "fx": {
    "status": "cached",
    "rate": "90.0000000000",
    "updated_at": "2026-09-25",
    "source": "cbr"
  },
  "supplier_email": {
    "status": "disabled"
  }
}
```

Error status codes: 401, 403, 422, 503. Gmail is worker heartbeat: connected, reauth_required, bad_credentials, network_unavailable, database_unavailable, worker_error, worker_stale or unknown. Stale heartbeat is not healthy. MoySklad configured means credentials present, not a live probe; Telegram configured means validated group configuration. FX cached is not a live quote; display date. No provider call is triggered by Settings.


## GET /buying/picker/pickups

Auth: Bearer session. Allowed role: manager or picker.

Request: none (no body).

Response JSON (200):
```json
{
  "pickups": [
    {
      "id": 1,
      "number": "B-000001",
      "supplier_name": "Synthetic supplier",
      "pickup_address": "Test street 1",
      "phone": "+70000000000",
      "pickup_notes": "Call on arrival",
      "items": [
        {
          "name": "Synthetic perfume 100 ml",
          "quantity": 2
        }
      ],
      "sent_at": "2026-09-25T12:00:00Z",
      "status": "sent",
      "received_at": null,
      "received_by_user_id": null,
      "received_by_role": null,
      "received_by_username": null
    }
  ],
  "total": 1,
  "offset": 0,
  "limit": 50
}
```

Error status codes: 401, 403, 422, 503. Only sent purchases. Query: offset>=0, limit=1..100. All employees see the shared list; drafts/errors/cancellations excluded.


## GET /buying/picker/history

Auth: Bearer session. Allowed role: manager or picker.

Request: none (no body).

Response JSON (200):
```json
{
  "pickups": [
    {
      "id": 1,
      "number": "B-000001",
      "supplier_name": "Synthetic supplier",
      "pickup_address": "Test street 1",
      "phone": "+70000000000",
      "pickup_notes": "Call on arrival",
      "items": [
        {
          "name": "Synthetic perfume 100 ml",
          "quantity": 2
        }
      ],
      "sent_at": "2026-09-25T12:00:00Z",
      "status": "received",
      "received_at": "2026-09-25T12:00:00Z",
      "received_by_user_id": 2,
      "received_by_role": "picker",
      "received_by_username": "buying_picker"
    }
  ],
  "total": 1,
  "offset": 0,
  "limit": 50
}
```

Error status codes: 401, 403, 422, 503. Only received purchases. Query: offset>=0, limit=1..100. All employees see the shared list; drafts/errors/cancellations excluded.


## GET /buying/picker/pickups/{purchase_id}

Auth: Bearer session. Allowed role: manager or picker.

Request: none (no body).

Response JSON (200):
```json
{
  "id": 1,
  "number": "B-000001",
  "supplier_name": "Synthetic supplier",
  "pickup_address": "Test street 1",
  "phone": "+70000000000",
  "pickup_notes": "Call on arrival",
  "items": [
    {
      "name": "Synthetic perfume 100 ml",
      "quantity": 2
    }
  ],
  "sent_at": "2026-09-25T12:00:00Z",
  "status": "sent",
  "received_at": null,
  "received_by_user_id": null,
  "received_by_role": null,
  "received_by_username": null
}
```

Error status codes: 401, 403, 422, 503, 404. Only sent/received purchases are visible. Explicit response allowlist contains no prices, currency, email, replies or external IDs.


## POST /buying/picker/pickups/{purchase_id}/received

Auth: Bearer session. Allowed role: manager or picker.

Request: none (no body).

Response JSON (200):
```json
{
  "id": 1,
  "number": "B-000001",
  "supplier_name": "Synthetic supplier",
  "pickup_address": "Test street 1",
  "phone": "+70000000000",
  "pickup_notes": "Call on arrival",
  "items": [
    {
      "name": "Synthetic perfume 100 ml",
      "quantity": 2
    }
  ],
  "sent_at": "2026-09-25T12:00:00Z",
  "status": "received",
  "received_at": "2026-09-25T12:00:00Z",
  "received_by_user_id": 2,
  "received_by_role": "picker",
  "received_by_username": "buying_picker"
}
```

Error status codes: 401, 403, 422, 503, 404, 409. The Забрал action. Actor comes from authenticated account, never request JSON. Repeated action retains first actor/time and one event.
