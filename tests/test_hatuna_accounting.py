"""Hatuna accounting contract: exact Tilda retail price, no hidden provider writes."""
import asyncio
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from app.integrations.moysklad.documents import FakeDocumentAdapter, verified_document
from app.services.hatuna_accounting import (
    HatunaItem, HatunaPreflightError, hatuna_lines, hatuna_operation_keys,
    provisional_cost, stock_entry_intent, verify_hatuna_counterparty,
)

ORG, HATUNA, X_STORE, ITEM_A, ITEM_B, PURCHASE = [str(uuid4()) for _ in range(6)]


def _items():
    return [
        HatunaItem(product_id=ITEM_A, quantity=2, sale_unit_minor=490_000, source_item_id=21),
        HatunaItem(product_id=ITEM_B, quantity=1, sale_unit_minor=650_000, source_item_id=22),
    ]


def _args():
    return dict(order_id=13, x_plan_id=5, organization_id=ORG,
                x_store_id=X_STORE, items=_items(),
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


def test_one_priced_internal_entry_no_supplier_documents():
    intent = stock_entry_intent(**_args())
    assert intent["entity"] == "enter"
    doc = intent["document"]
    assert doc["store"]["meta"]["href"].endswith("/store/" + X_STORE)
    assert doc["organization"]["meta"]["href"].endswith("/organization/" + ORG)
    assert doc["applicable"] is True
    assert "Хатуна" in doc["description"]
    assert "13" in doc["description"]
    assert "предварительная" in doc["description"]
    assert [i["price"] for i in doc["positions"]] == [390_000, 550_000]
    assert [i["quantity"] for i in doc["positions"]] == [2, 1]
    assert "agent" not in doc and "purchaseOrder" not in doc
    assert stock_entry_intent(**_args()) == intent


@pytest.mark.parametrize("invalid", [
    {"picker_confirmed": False},
    {"picker_confirmed": "yes"},
    {"organization_id": "unknown"},
    {"x_store_id": "unknown"},
    {"items": []},
    {"items": [HatunaItem(product_id="invalid", quantity=1,
                          sale_unit_minor=400_000, source_item_id=1)]},
    {"items": [HatunaItem(product_id=ITEM_A, quantity=1,
                          sale_unit_minor=100_000, source_item_id=1)]},
    {"items": [HatunaItem(product_id=ITEM_A, quantity=2,
                          sale_unit_minor=400_000, source_item_id=1),
               HatunaItem(product_id=ITEM_B, quantity=3,
                          sale_unit_minor=500_000, source_item_id=1)]},
])
def test_no_entry_without_verified_picker_catalog_and_price(invalid):
    with pytest.raises(HatunaPreflightError):
        stock_entry_intent(**{**_args(), **invalid})


def test_one_key_per_order_and_x_warehouse_plan():
    one = hatuna_operation_keys(13, 5)
    assert one == hatuna_operation_keys(13, 5)
    assert one != hatuna_operation_keys(13, 6)
    assert one != hatuna_operation_keys(14, 5)
    assert set(one) == {"receipt"}
    assert one["receipt"].endswith(":enter")
    assert one["receipt"] != "oms:hatuna:order:13:x-plan:5:receipt"


def test_fake_provider_creates_only_one_enter_with_priced_positions():
    adapter = FakeDocumentAdapter()
    intent = stock_entry_intent(**_args())
    async def exercise():
        result = await adapter.create(intent)
        assert verified_document(intent, result)
        assert (await adapter.find(intent))[0]["id"] == result["id"]
        assert adapter.calls == 1
    asyncio.run(exercise())


def test_entry_rejects_zero_or_invalid_acquisition_prices():
    from app.integrations.moysklad.payloads import hatuna_enter
    with pytest.raises(ValueError):
        hatuna_enter(ORG, X_STORE, [{"id": ITEM_A, "qty": 1, "price": -1}], "test:hatuna", 13)
    with pytest.raises(ValueError):
        hatuna_enter(ORG, X_STORE, [{"id": ITEM_A, "qty": 1, "price": 0}], "test:hatuna", 13)
    with pytest.raises(ValueError):
        hatuna_enter(ORG, X_STORE, [{"id": ITEM_A, "qty": 1}], "test:hatuna", 13)


def test_hatuna_counterparty_identity_is_verified_readonly():
    class StubClient:
        def __init__(self, data):
            self.data = data
            self.calls = []
        def get_counterparty(self, identifier):
            self.calls.append(identifier)
            return self.data

    async def run():
        good = StubClient({"id": HATUNA, "name": "Хатуна", "archived": False})
        assert await verify_hatuna_counterparty(HATUNA, client=good) is True
        assert good.calls == [HATUNA]
        for invalid in [
            {"id": HATUNA, "name": "Поставщик не Хатуна", "archived": True},
            {"id": HATUNA, "name": "Другая компания", "archived": False},
            {"id": ORG, "name": "Хатуна", "archived": False},
        ]:
            with pytest.raises(HatunaPreflightError):
                await verify_hatuna_counterparty(HATUNA, client=StubClient(invalid))
        broken = StubClient({"id": HATUNA, "name": "Хатуна"})
        with pytest.raises(HatunaPreflightError):
            await verify_hatuna_counterparty("not-a-uuid", client=broken)
        assert broken.calls == []
    asyncio.run(run())
