from types import SimpleNamespace

from app.bot.manager_ui_flags import enabled
from app.services.manager_workspace import (
    closed_order_card,
    is_closed_order,
    order_keyboard,
)


def _order(**overrides):
    values = dict(
        id=15,
        revision=3,
        source="email",
        fulfillment_status="shipped",
        payment_status="paid",
        delivery_status="pending",
        customer_name="Test Client",
        total=540000,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_new_manager_ui_is_off_by_default(monkeypatch):
    monkeypatch.delenv("OMS_MANAGER_UI_V2_ENABLED", raising=False)
    assert enabled() is False


def test_completed_email_order_collapses_with_home(monkeypatch):
    monkeypatch.setenv("OMS_MANAGER_UI_V2_ENABLED", "true")
    order = _order()
    assert is_closed_order(order)
    controls = order_keyboard(order)["inline_keyboard"]
    assert [button["text"] for row in controls for button in row] == [
        "📄 Подробнее", "🏠 Главное меню",
    ]
    assert controls[-1][0]["callback_data"] == "workspace:home"
    assert "№15" in closed_order_card(order)


def test_completed_email_has_readonly_expanded_controls(monkeypatch):
    monkeypatch.setenv("OMS_MANAGER_UI_V2_ENABLED", "true")
    controls = order_keyboard(_order(), expanded=True)["inline_keyboard"]
    callbacks = [button["callback_data"] for row in controls for button in row]
    assert "workspace:home" in callbacks
    assert all(":paid:" not in entry and ":shipped:" not in entry for entry in callbacks)


def test_shipped_unpaid_email_not_closed():
    assert not is_closed_order(_order(payment_status="unpaid"))


def test_tilda_requires_delivery_confirmation():
    assert not is_closed_order(_order(source="website", delivery_status="dispatched"))
    assert is_closed_order(_order(source="website", delivery_status="delivered"))


def test_order_summary_is_readonly():
    order = _order()
    before = dict(vars(order))
    closed_order_card(order)
    assert vars(order) == before
