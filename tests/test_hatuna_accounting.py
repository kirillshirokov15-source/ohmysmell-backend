"""Hatuna accounting contract: exact Tilda retail price, no hidden provider writes."""
import asyncio
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from app.integrations.moysklad.documents import FakeDocumentAdapter, verified_document
from app.services.hatuna_accounting import (
    HatunaItem, HatunaPreflightError, hatuna_lines, hatuna_operation_keys,
    provisional_cost, purchase_intent, receipt_intent,
)

ORG, HATUNA, X_STORE, ITEM_A, ITEM_B, PURCHASE = [str(uuid4()) for _ in range(6)]


def _items():
    return [
        HatunaItem(product_id=ITEM_A, quantity=2, sale_unit_minor=490_000, source_item_id=21),
        HatunaItem(product_id=ITEM_B, quantity=1, sale_unit_minor=650_000, source_item_id=22),
    ]


def _args():
    return dict(order_id=13, x_plan_id=5, organization_id=ORG,
                hatuna_id=HATUNA, x_store_id=X_STORE, items=_items(),
                picker_confirmed=True)


def test_1000_rub_discount_is_per_unit_and_never_changes_retail():
    for sale, cost in [(150_000, 50_000), (490_000, 390_000), (1_000_000, 900_000)]:
        assert provisional_cost(sale) == cost
    lines = hatuna_lines(_items())
    assert [(r["qty"], r["price"]) for r in lines] == [(2, 390_000), (1, 550_000)]
    assert sum(r["qty"] * r["price"] for r in lines) == 1_330_000
    assert sum(i.quantity * i.sale_unit_minor for i in _items()) == 1_630_000
    assert sum(i.quantity * i.sale_unit_minor for i in _items()) - sum(
        r["qty"] * r["price"] for r in lines) == 300_000
    with pytest.raises(FrozenInstanceError):
        _items()[0].sale_unit_minor = 42


@pytest.mark.parametrize("bad", [0, 99_999, 100_000, -42, 12.5, True, "200000", None])
def test_nonpositive_estimated_cost_requires_manual_review(bad):
    with pytest.raises(HatunaPreflightError):
        provisional_cost(bad)


def test_supplier_order_then_linked_receipt_with_same_cost_and_inventory():
    po = purchase_intent(**_args())
    assert po["entity"] == "purchaseorder"
    assert po["document"]["agent"]["meta"]["href"].endswith("/counterparty/" + HATUNA)
    assert po["document"]["store"]["meta"]["href"].endswith("/store/" + X_STORE)
    assert po["document"]["description"].startswith("Внутренний учёт")
    receipt = receipt_intent(**_args(), purchase_id=PURCHASE)
    assert receipt["entity"] == "supply"
    assert receipt["document"]["purchaseOrder"]["meta"]["href"].endswith("/purchaseorder/" + PURCHASE)
    assert receipt["document"]["positions"] == po["document"]["positions"]
    assert po["document"]["externalCode"] != receipt["document"]["externalCode"]
    assert po["document"]["syncId"] != receipt["document"]["syncId"]
    assert purchase_intent(**_args()) == po
    assert receipt_intent(**_args(), purchase_id=PURCHASE) == receipt
    assert po["document"]["applicable"] is True
    assert receipt["document"]["applicable"] is True


@pytest.mark.parametrize("invalid", [
    {"picker_confirmed": False},
    {"picker_confirmed": "yes"},
    {"hatuna_id": "not-an-uuid"},
    {"x_store_id": "unknown"},
    {"items": []},
    {"items": [HatunaItem(product_id="not-an-uuid", quantity=1,
                          sale_unit_minor=400_000, source_item_id=1)]},
    {"items": [HatunaItem(product_id=ITEM_A, quantity=1,
                          sale_unit_minor=100_000, source_item_id=1)]},
    {"items": [HatunaItem(product_id=ITEM_A, quantity=2,
                          sale_unit_minor=400_000, source_item_id=1),
               HatunaItem(product_id=ITEM_B, quantity=3,
                          sale_unit_minor=500_000, source_item_id=1)]},
])
def test_provider_intent_refuses_unconfirmed_or_unmatched_x_items(invalid):
    with pytest.raises(HatunaPreflightError):
        purchase_intent(**{**_args(), **invalid})


def test_idempotent_x_keys_are_per_order_and_per_warehouse_plan():
    one = hatuna_operation_keys(13, 5)
    assert one == hatuna_operation_keys(13, 5)
    assert one != hatuna_operation_keys(13, 6)
    assert one != hatuna_operation_keys(14, 5)
    assert one["purchase"] != one["receipt"]


def test_fake_moysklad_adapter_reconciles_intents():
    # No credentials and no remote network. Both docs share the same X lines.
    adapter = FakeDocumentAdapter()
    po = purchase_intent(**_args())
    receipt = receipt_intent(**_args(), purchase_id=PURCHASE)
    async def exercise():
        result_po = await adapter.create(po)
        result_receipt = await adapter.create(receipt)
        assert verified_document(po, result_po)
        assert verified_document(receipt, result_receipt)
        assert (await adapter.find(po))[0]["id"] == result_po["id"]
        assert (await adapter.find(receipt))[0]["id"] == result_receipt["id"]
        assert adapter.calls == 2
    asyncio.run(exercise())
