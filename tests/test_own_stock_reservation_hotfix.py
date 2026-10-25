"""Order #9 regression: OWN-only physical picking may create real reserve,
but must never create a second reserve or silently accept ambiguous outcomes.
No MoySklad network calls are made by these tests.
"""
import asyncio
from types import SimpleNamespace as O
from unittest.mock import AsyncMock
import pytest

from app.config.settings import settings
from app.models.order import Order
from app.models.fulfillment import ExternalOperation
from app.services import own_stock_reservation as own
from app.services.order_desk import DeskError
from app.services.manager_workspace import order_keyboard


def snapshot(*, status="assembled", buyer="moysklad-buyer", source="own",
             ledger=None):
    order = O(id=9, source="website", manual_fulfillment=True, status="new",
              needs_review=False, fulfillment_status=status, counterparty_id=buyer,
              revision=4, delivery_method="pickup")
    allocation = O(id=321, order_item_id=91, qty=1,
                   managed_source=True, source_type=source,
                   supply_status="confirmed")
    plan = O(id=123, order_id=9, status="planned", allocations=[allocation])
    return order, plan, ledger


def fixture(monkeypatch, *, status="assembled", buyer="buyer", source="own", ledger=None):
    order, plan, record = snapshot(status=status, buyer=buyer, source=source, ledger=ledger)
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "external_writes_enabled", True)
    monkeypatch.setattr("app.services.hatuna_post_pick.post_pick_enabled_for_order", lambda _: True)
    monkeypatch.setattr(own, "source_plans", AsyncMock(return_value=[plan]))

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def scalar(self, statement):
            # A successful external reserve in this fixture already has an
            # associated durable local OrderEvent.
            return 1
        async def get(self, model, key):
            if model is Order: return order
            if model is ExternalOperation:
                if key.endswith(":reserve"): return record
                return None
            raise AssertionError("Unexpected model")
    monkeypatch.setattr(own, "async_session", Session)
    receiver = O(act=AsyncMock(return_value={"id":"provider-doc","revision":5}))
    monkeypatch.setattr(own, "WarehouseDocumentService", lambda: receiver)
    return receiver


def test_post_assembly_own_reserve_created_once(monkeypatch):
    writer = fixture(monkeypatch)
    result = asyncio.run(own.reserve_assembled_own_order(9, 111, -100123456))
    assert result == {"reserved":True,"created":1}
    writer.act.assert_awaited_once_with(9,111,-100123456,4,"reserve",123)


def test_existing_succeeded_document_not_reposted(monkeypatch):
    writer = fixture(monkeypatch,ledger=O(status="succeeded"))
    assert asyncio.run(own.reserve_assembled_own_order(9,111,-100123456))["created"]==0
    writer.act.assert_not_awaited()


@pytest.mark.parametrize("status,buyer,source", [
    ("new","buyer","own"),("assembled",None,"own"),
    ("assembled","buyer","partner_x"),("assembled","buyer","external"),
])
def test_unsafe_reserve_is_rejected_without_writes(monkeypatch,status,buyer,source):
    writer=fixture(monkeypatch,status=status,buyer=buyer,source=source)
    with pytest.raises(DeskError):
        asyncio.run(own.reserve_assembled_own_order(9,111,-100123456))
    writer.act.assert_not_awaited()


def test_uncertain_document_does_not_retry(monkeypatch):
    writer=fixture(monkeypatch,ledger=O(status="uncertain"))
    with pytest.raises(DeskError,match="создание нового документа запрещено"):
        asyncio.run(own.reserve_assembled_own_order(9,111,-100123456))
    writer.act.assert_not_awaited()


def test_assembled_tilda_button_prefers_warehouse_and_no_review(monkeypatch):
    monkeypatch.setattr("app.services.hatuna_post_pick.post_pick_enabled_for_order",lambda _:True)
    order=O(id=9, revision=7, fulfillment_status="assembled",
        status="new", manual_fulfillment=True, needs_review=False,
        delivery_method="pickup",payment_status="paid",
        delivery_status="pending",desk_draft_id=136)
    keyboard=order_keyboard(order, [], expanded=False)["inline_keyboard"]
    assert keyboard[0][0]["callback_data"]=="order:buyer:9:7"
    expanded=str(order_keyboard(order, [], expanded=True))
    assert "Проблема / требуется проверка" not in expanded


def test_dispatch_creates_demand_only_after_verified_reserve(monkeypatch):
    writer = fixture(monkeypatch, ledger=O(status="succeeded"))
    result = asyncio.run(own.dispatch_assembled_own_order(9, 111, -100123456))
    assert result == {"documents_created":1}
    writer.act.assert_awaited_once_with(9,111,-100123456,4,"ship",123)


def test_dispatch_missing_reserve_does_not_fake_shipment(monkeypatch):
    writer = fixture(monkeypatch, ledger=None)
    with pytest.raises(DeskError, match="резерв"):
        asyncio.run(own.dispatch_assembled_own_order(9,111,-100123456))
    writer.act.assert_not_awaited()
