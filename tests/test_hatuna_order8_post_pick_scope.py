"""Order-scoped physical picking: no change to unrelated production orders."""
from types import SimpleNamespace
from app.services.hatuna_post_pick import post_pick_enabled_for_order


def test_flags_stay_disabled_without_explicit_global_optin(monkeypatch):
    monkeypatch.delenv("OMS_HATUNA_POST_PICK_ENABLED", raising=False)
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ORDER_IDS", "8")
    assert post_pick_enabled_for_order(8) is False


def test_only_order8_gets_physical_picking_with_allowlist(monkeypatch):
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "true")
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ORDER_IDS", "8")
    assert post_pick_enabled_for_order(8) is True
    assert post_pick_enabled_for_order(7) is False
    assert post_pick_enabled_for_order(9) is False


def test_invalid_allowlist_fails_closed(monkeypatch):
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "true")
    for value in ("8,garbage", "0", "-8", "all", "8;9", "8,"):
        monkeypatch.setenv("OMS_HATUNA_POST_PICK_ORDER_IDS", value)
        assert post_pick_enabled_for_order(8) is False


def test_empty_allowlist_keeps_existing_test_and_global_contract(monkeypatch):
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "true")
    monkeypatch.delenv("OMS_HATUNA_POST_PICK_ORDER_IDS", raising=False)
    assert post_pick_enabled_for_order(8) is True
    assert post_pick_enabled_for_order(9) is True


def test_external_documents_remain_separately_gated(monkeypatch):
    from app.services.hatuna_document_runner import HatunaDocumentRunner
    from app.config.settings import settings
    from app.services.hatuna_accounting import HatunaItem, HatunaPreflightError
    from uuid import uuid4
    import asyncio
    import pytest
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "true")
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ORDER_IDS", "8")
    monkeypatch.setenv("OMS_HATUNA_AUTO_WRITES_ENABLED", "false")
    monkeypatch.setattr(settings, "external_writes_enabled", False)
    monkeypatch.setattr(settings, "environment", "production")
    item=HatunaItem(product_id=str(uuid4()), quantity=1, sale_unit_minor=350000, source_item_id=12)
    async def attempt():
        await HatunaDocumentRunner().apply(
            order_id=8, x_plan_id=9,
            organization_id=str(uuid4()), x_store_id=str(uuid4()),
            items=[item], picker_confirmed=True)
    with pytest.raises(HatunaPreflightError, match="не активировано"):
        asyncio.run(attempt())


def test_order8_picking_does_not_need_x_receipt(monkeypatch):
    from app.services.hatuna_post_pick import require_complete_picking_plan
    from app.services.order_operations import OperationError
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "true")
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ORDER_IDS", "8")
    order = SimpleNamespace(id=8, manual_fulfillment=True,
        items=[SimpleNamespace(id=11, qty=1), SimpleNamespace(id=12, qty=1)])
    own = SimpleNamespace(order_item_id=11, qty=1, source_type="own",
                          managed_source=True, supply_status="confirmed")
    x = SimpleNamespace(order_item_id=12, qty=1, source_type="partner_x",
                        managed_source=True, supply_status="awaiting_receipt")
    plans=[SimpleNamespace(status="planned", allocations=[own]),
           SimpleNamespace(status="planned", allocations=[x])]
    assert require_complete_picking_plan(order, plans)
    x.qty=2
    with __import__('pytest').raises(OperationError, match="Распределите"):
        require_complete_picking_plan(order, plans)
