# Warehouse policy for website orders: OWN + X (2026-10-14)

## Separate the sales route from procurement

For Tilda retail orders the only permitted source types are:
- `own`: inventory on a main, non-X MoySklad warehouse, with verified free stock;
- `partner_x`: **our own physical `Склад Х`**, represented by the legacy enum but never by an external supplier, Buying purchase or invented supplier cost.

The same order may be divided between these two sources and across warehouse-specific shipments.

`external` and `BuyingPurchase` are not selectable for new Tilda allocations.
The standalone Buying site remains the exclusive procurement workflow for external suppliers.
Do not change/cancel any historical external allocations automatically; reconcile them manually.

## When a Tilda product does not exist in MoySklad

1. Preserve checkout, item name, amount and quantity in the Tilda draft and customer bot even if SKU lookup fails.
2. Mark the draft for review: physically check X and search MoySklad by SKU/name to avoid duplicate cards.
3. If no matching MoySklad product exists, create a **verified** product card in a separate, authorized workflow; do not auto-create it from the Tilda title.
4. Recheck the Tilda-to-MoySklad mapping, original retail prices, actual warehouse ID and physical quantity.
5. Confirm receipt and existing balance consistency. No automatic inventory increase based on a website order.
6. Only then allocate to `Склад Х`, post a permitted idempotent receipt (when actually appropriate), reserve and ship.

`OMS_OWN_X_LIVE_ENABLED` remains off until inventory valuation and duplicate physical receipts are verified. No live MoySklad documents may be generated in development/CI/staging tests.

## Enforcement in this feature branch

Staging/production `OrderSourceService._assign` rejects external Tilda allocations **before** any writes, external-purchase locks or purchase creation, including mixed requests via the internal API. It also validates that OWN is not assigned to the physical X warehouse and X is not assigned to a different warehouse. Manager Telegram inline choices and `/sourceadd` exclude external suppliers; expired/stale supplier callbacks are rejected. Historical allocations remain visible.

This is feature-branch work until the split manager-bot and backend production services are upgraded consistently and real E2E acceptance is performed. Buying functionality itself is unchanged.
