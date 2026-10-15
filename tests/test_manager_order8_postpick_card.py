"""Regression for manager card during Tilda X physical-picking versus accounting."""
from types import SimpleNamespace

import pytest

from app.config.settings import settings
from app.services.manager_workspace import order_card


def case(*, order_id=8, stage="assembling"):
    items = [
        SimpleNamespace(id=11, name="Boadicea Ardent Set", qty=1, price=550000),
        SimpleNamespace(id=12, name="Charlotte Tilbury Spray 100 ml", qty=1, price=350000),
    ]
    def allocation(item_id, source, state):
        return SimpleNamespace(
            order_item_id=item_id, source_type=source, supply_status=state,
            managed_source=True, qty=1, receipt_reference=None,
            receipt_simulated=False, buying_purchase=None)
    plans = [
        SimpleNamespace(status="planned", allocations=[allocation(11, "own", "confirmed")]),
        SimpleNamespace(status="planned", allocations=[allocation(12, "partner_x", "awaiting_receipt")]),
    ]
    order = SimpleNamespace(
        id=order_id, status="new", customer_name="TEST OMS CRM", source="website",
        phone="+7 000 000 00 00", items=items, total=900000,
        manual_fulfillment=True, needs_review=False, fulfillment_status=stage,
        payment_status="paid", delivery_status="pending")
    return order, plans


@pytest.mark.parametrize("stage,expected", [
    ("new", "сверить физическое наличие всех назначенных товаров"),
    ("assembling", "подтвердить «Заказ собран»"),
    ("assembled", "выполнить проверку post-pick"),
    ("shipped", "сверить документы отгрузки"),
])
def test_order8_x_post_pick_has_correct_next_step(monkeypatch, stage, expected):
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "true")
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ORDER_IDS", "8")
    monkeypatch.setattr(settings, "external_writes_enabled", False)
    order, plans = case(stage=stage)
    card = order_card(order, plans)
    assert "Распределено по складам: 2 из 2 шт." in card
    assert "Физическое наличие подтверждает сборщик" in card
    assert "готово к сборке: 1 из 2" not in card.lower()
    assert "резерв → сборка" not in card
    assert expected in card
    assert "назначено, сборщик проверяет наличие" in card or "ожидает внутреннего оприходования" in card


def test_other_orders_keep_existing_warehouse_accounting_labels(monkeypatch):
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "true")
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ORDER_IDS", "8")
    order, plans = case(order_id=9)
    card = order_card(order, plans)
    assert "Готово к сборке: 1 из 2 шт." in card
    assert "ожидает поступления" in card
    assert "подтвердить физическое получение" in card
    assert "Распределено по складам:" not in card


def test_post_pick_requires_explicit_feature_flag(monkeypatch):
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "false")
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ORDER_IDS", "8")
    order, plans = case()
    assert "Готово к сборке: 1 из 2 шт." in order_card(order, plans)
