"""X is our physical warehouse: no supplier, no procurement price.

Pure payload checks do not perform provider writes.
"""
import pytest

from app.integrations.moysklad.payloads import stock_enter, direct_demand
from app.integrations.moysklad.documents import FakeDocumentAdapter, verified_document
from app.services.x_own_warehouse import (
    x_autoflow_enabled, x_enter_key, x_demand_key, test_order,
)
from app.models.supply import Supplier


ORG="11111111-1111-4111-8111-111111111111"
BUYER="22222222-2222-4222-8222-222222222222"
STORE="33333333-3333-4333-8333-333333333333"
ITEM="44444444-4444-4444-8444-444444444444"


def test_own_x_enter_has_no_supplier_purchase_cost_or_sale_price():
    key=x_enter_key(19)
    doc=stock_enter(ORG,STORE,[{"id": ITEM,"qty": 1}],key)
    assert doc["store"]["meta"]["href"].endswith("/store/"+STORE)
    assert doc["organization"]["meta"]["href"].endswith("/organization/"+ORG)
    assert doc["positions"][0]["quantity"] == 1
    assert "price" not in doc["positions"][0]
    assert "agent" not in doc
    assert "purchaseOrder" not in doc
    assert "customerOrder" not in doc
    assert doc["externalCode"] == key and doc["applicable"] is True
    assert verified_document({"entity":"enter","document":doc},{**doc,"id":"test-1"})
    # Even if provider includes a default zero valuation, reconciliation can
    # accept zero. A different valuation must be investigated separately.
    zero={**doc,"id":"test-2","positions":[{**doc["positions"][0],"price":0}]}
    assert verified_document({"entity":"enter","document":doc},zero)
    wrong={**doc,"id":"test-3","positions":[{**doc["positions"][0],"price":550000}]}
    assert not verified_document({"entity":"enter","document":doc},wrong)


def test_direct_demand_uses_immutable_tilda_retail_price_and_real_buyer():
    key=x_demand_key(7)
    doc=direct_demand(ORG,BUYER,STORE,[{"id":ITEM,"qty":1,"price":550000}],key)
    assert doc["positions"][0]["price"] == 550000
    assert doc["agent"]["meta"]["href"].endswith("/counterparty/"+BUYER)
    assert "purchaseOrder" not in doc
    assert "customerOrder" not in doc
    assert doc["applicable"] is True and doc["externalCode"] == key
    assert verified_document({"entity":"demand","document":doc},{**doc,"id":"test-demand"})


@pytest.mark.parametrize("qty", [0,-1,1.5,"1",True])
def test_enter_rejects_noninteger_or_nonpositive_quantity(qty):
    with pytest.raises(ValueError):
        stock_enter(ORG,STORE,[{"id":ITEM,"qty":qty}],x_enter_key(99))


def test_test_orders_always_excluded_even_when_bot_ready():
    from types import SimpleNamespace
    assert test_order(SimpleNamespace(customer_name="TEST OMS CRM 2",comment="",request_key="x"))
    assert test_order(SimpleNamespace(customer_name="Jane",comment="тестовый заказ",request_key=None))
    assert not test_order(SimpleNamespace(customer_name="Ольга",comment="доставка",request_key="real"))


def test_source_type_is_existing_db_enum_for_backwards_compatibility():
    from app.schemas.order_sources import SourcePart
    item=SourcePart(item_id=5,qty=1,source_type="partner_x",warehouse_id=STORE)
    assert item.base_cost_minor is None and item.supplier_id is None
