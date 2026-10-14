# Tilda → OWN / Hatuna → MoySklad (confirmed Oct 14, 2026)

## Authoritative business rules

Tilda holds the immutable **retail sale price** and quantity for every order item. The
customer bot sees those original item names. No sale-price changes are derived from
purchase prices.

Website orders have two sources:
- **OWN**: the main physical warehouse in MoySklad; reserve its available stock and
  create shipment from that warehouse.
- **X / Hatuna**: Hatuna is an accounting supplier/counterparty and X is a physical
  warehouse. Hatuna never needs to confirm, be emailed, or use Buying. The picker
  must physically assemble and confirm that all assigned X items are present.

**After** trusted picker confirmation, the unattended document sequence for X is:

1. Verify actual product identities (MoySklad UUIDs), X store ID, the specific
   existing Hatuna counterparty ID, allocation quantities, customer order, and
   on-hand stock reconciliation. If an item is absent from the MoySklad product
   catalog, match by an exact identifier or create a verified product card without
   duplicates **before** creating inventory documents.
2. Create one `purchaseorder` per customer order / X warehouse plan, including
   multiple X items. Unit estimated cost = Tilda *unit* retail price − RUB 1,000;
   no customer sale price is modified.
3. Create `supply` (приёмка), associated with that `purchaseorder`, into X.
   Only posted stock receipt changes X on-hand stock.
4. Create a reservation for the customer’s X allocation, then one `demand`
   shipment originating from X.
5. Create a distinct reservation and `demand` for OWN items. Where there are
   mixed allocations, allow separate fulfillment documents and associate all
   documents with one client order and its original Tilda price and quantities.
6. Expose completed, failed or reconciliation-required step statuses to manager.
   Payment tracking remains independent and manually confirmed.

**These steps require durable external-operation journal keys, provider-side
exactly-once reconciliation after timeouts, and an independently retryable worker
or queue; simply calling APIs from the Telegram handler is unsafe.**

## Month-end bookkeeping

Every Hatuna purchase and receipt must be marked provisional and preserve its
source Tilda unit price, estimated unit cost, quantity, and supplier UUID in the
audit. At month end the manager compares each cost to the real settlement and
records an approved adjustment in MoySklad; a blanket overwrite would corrupt
historical inventory valuation. Retail sale prices never change during this audit.

Automatic cost is blocked if website unit price is at most RUB 1,000: a negative
or zero receipt cost silently produced by the formula would be incorrect. The
client order is retained for manager review instead.

**Existing X stock must be reconciled before posting**. If units were already
received in MoySklad, do not post them a second time merely because they were
assigned to a new website order. Reuse existing recorded stock where appropriate
and create accounting purchases only for genuinely unrecorded units.

## Implementation checkpoint

- `app/services/hatuna_accounting.py` builds **pure**, deterministic purchase
  and linked receipt intents from picker-confirmed X lines. It has no I/O.
- `app/integrations/moysklad/payloads.py::purchaseorder` builds the supplier
  order with provisional acquisition prices.
- Fake MoySklad adapter tests validate price arithmetic, line grouping, linked
  documents, dedup keys and rejection of unconfirmed/unmatched inputs.

**Not implemented yet:** durable post-picking worker trigger, live document
execution, recovery/reconciliation UX, per-pick confirmation and missing-product
catalog creation. Production and warehouse-write flags must remain unchanged
until all are implemented and verified in staging. Do not enable X receipt using
the old `enter` path: that path lacks Hatuna as counterparty.
