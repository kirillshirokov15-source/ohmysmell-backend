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

The manager bot uses the existing `Начать сборку` → `Заказ собран`
actions as the trusted physical picking boundary for now. The dedicated
`hatuna_auto` worker can, when expressly enabled, detect the committed
`assembled` state and perform purchaseorder → linked supply →
reservation/shipments for each warehouse, then update the order to shipped.
The old `enter` receipt path is NOT used by this worker.

- `OMS_HATUNA_POST_PICK_ENABLED=true` enables source coverage before picking.
- `OMS_HATUNA_AUTO_PROCESS_ENABLED=true` starts the background worker on the
  manager service (must never be enabled before synthetic staging acceptance).
- `OMS_HATUNA_COUNTERPARTY_ID` must contain the real, verified MoySklad Hatuna UUID.
- `OMS_HATUNA_AUTO_WRITES_ENABLED=true` plus
  `EXTERNAL_WRITES_ENABLED=true` are BOTH required for live supplier documents.
- `/hatuna ORDER_ID` shows the responsible manager the original sale total,
  provisional purchase costs, and recorded purchase/receipt/reservation/shipment
  IDs with reconciliation states.

If the provider response is lost or ambiguous, the persisted
`external_operations` entry blocks retry and requires explicit inspection.

**Not implemented yet:** automatic creation of MoySklad product cards absent
from the catalog, a dedicated Picker-role Telegram confirmation (currently
authorized manager confirmation), and reconciliation of already-posted X stock
with previously unrecorded Hatuna purchases. No production flags are enabled.

Production and warehouse-write flags must remain unchanged until the
remaining cases, provider read-only mappings, and staged E2E acceptance pass.
Never enable the obsolete `enter` path for Hatuna.
