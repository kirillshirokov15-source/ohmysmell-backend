"""Gmail orders: the manual Paid marker is last, not a shipping prerequisite.

Tilda retains its payment-first UX, Gmail has no delivery stage, and neither
channel creates financial provider documents from the Paid action.
"""
from datetime import datetime, timezone
from types import SimpleNamespace as Obj

import pytest

from app.services.manager_workspace import available_order_keyboard, order_keyboard
from app.services.order_operations import OrderAction, apply_action


def order(source="email", stage="new", payment="unpaid"):
    return Obj(
        id=202, revision=5, status="new", source=source,
        payment_status=payment, fulfillment_status=stage,
        delivery_status="pending", delivery_method=(
            "unselected" if source == "email" else "pickup"),
        needs_review=False, manual_fulfillment=True,
        counterparty_id="known-provider-buyer",
        items=[Obj(id=4, name="Product", qty=1, price=50000, item_total=50000)],
        ui_supply_ready=True, ui_cancellable=True,
        desk_draft_id=121, paid_at=None, paid_by_manager_id=None,
        payment_note=None,
    )


def actions(keyboard):
    return [button["callback_data"].split(":")[1]
            for row in keyboard["inline_keyboard"] for button in row]


@pytest.mark.parametrize("stage,expected_first", [
    ("new", "sources"),
    ("assembling", "sources"),
    ("assembled", "sources"),
    ("shipped", "actions"),
])
def test_gmail_paid_last_and_not_primary_before_shipment(monkeypatch, stage, expected_first):
    monkeypatch.setattr("app.services.hatuna_post_pick.post_pick_enabled_for_order",
                        lambda _: True)
    current=order(stage=stage)
    full=order_keyboard(current, [], expanded=True)
    compact=order_keyboard(current, [])
    direct=available_order_keyboard(current, [])
    for keyboard in (full, direct):
        assert actions(keyboard)[-1] == "paid", "Paid must be the final expanded action"
        assert actions(keyboard).count("paid") == 1
        assert not any(a.startswith("delivery") or a == "delivered"
                       for a in actions(keyboard)), "Email has no delivery controls"
    assert actions(compact)[0] == expected_first
    if stage == "shipped":
        assert actions(compact) == ["actions", "paid"]
    else:
        assert "paid" not in actions(compact), "Use the final expanded action until shipment"


@pytest.mark.parametrize("stage", ["new", "assembling", "assembled", "shipped"])
def test_gmail_paid_does_not_block_warehouse_work(stage, monkeypatch):
    monkeypatch.setattr("app.services.hatuna_post_pick.post_pick_enabled_for_order",
                        lambda _: True)
    current=order(stage=stage, payment="paid")
    for keyboard in (order_keyboard(current, []), order_keyboard(current, [], expanded=True)):
        assert "paid" not in actions(keyboard)
    if stage == "assembling":
        assert "assembled" in actions(order_keyboard(current, []))
    if stage == "assembled":
        assert "sources" in actions(order_keyboard(current, []))


@pytest.mark.parametrize("stage", ["new", "assembling", "assembled", "shipped"])
def test_tilda_keeps_paid_first_without_changing_delivery(stage,monkeypatch):
    monkeypatch.setattr("app.services.hatuna_post_pick.post_pick_enabled_for_order",
                        lambda _: True)
    current=order("website", stage)
    current.delivery_status = "dispatched" if stage == "shipped" else "pending"
    assert actions(order_keyboard(current, []))[0] == "paid"
    assert actions(order_keyboard(current, [], expanded=True))[0] == "paid"
    if stage == "shipped":
        assert "delivered" in actions(order_keyboard(current, [], expanded=True))


def test_manual_payment_is_still_independent_of_gmail_warehouse_and_delivery():
    current=order(stage="new")
    current.status_changed_at = None
    current.status_changed_by_manager_id = None
    now = datetime(2026,10,27,0,1,tzinfo=timezone.utc)
    apply_action(current,OrderAction(action="paid",expected_revision=5,
        idempotency_key="test-manual-gmail-payment"), manager_id=17,now=now)
    assert current.payment_status == "paid"
    assert current.paid_at == now
    assert current.fulfillment_status == "new"
    assert current.delivery_status == "pending"
    assert current.status_changed_at is None
