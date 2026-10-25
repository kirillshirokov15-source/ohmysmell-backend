"""Manager may confirm a received payment at any order stage.

This is a CRM-only status action, independent of fulfillment and delivery,
and never creates a MoySklad payment document.
"""
from datetime import datetime, timezone
from types import SimpleNamespace as Obj

import pytest

from app.services.manager_workspace import available_order_keyboard, order_keyboard
from app.services.order_operations import OrderAction, apply_action


def order(stage="new", *, payment="unpaid", delivered="pending", manual=True):
    return Obj(
        id=138, revision=7, status="new", source="website",
        payment_status=payment, fulfillment_status=stage,
        delivery_status=delivered, delivery_method="manual",
        needs_review=False, manual_fulfillment=manual,
        counterparty_id="verified-provider-buyer",
        items=[Obj(id=11, name="OWN", qty=1, price=10000)],
        ui_supply_ready=True, ui_cancellable=True,
        desk_draft_id=138, paid_at=None, paid_by_manager_id=None,
        payment_note=None,
    )


@pytest.mark.parametrize("stage,delivered", [
    ("new","pending"),
    ("assembling","pending"),
    ("assembled","pending"),
    ("shipped","dispatched"),
    ("shipped","delivered"),
])
def test_paid_first_in_primary_and_expanded_menu_independent_of_fulfillment(monkeypatch,stage,delivered):
    monkeypatch.setattr("app.services.hatuna_post_pick.post_pick_enabled_for_order",
                        lambda _id: True)
    current=order(stage,delivered=delivered)
    compact=order_keyboard(current,[])["inline_keyboard"]
    expanded=order_keyboard(current,[],expanded=True)["inline_keyboard"]
    assert compact[0][0] == {
        "text":"Оплачен", "callback_data":"order:paid:138:7"
    }
    assert expanded[0][0]["callback_data"]=="order:paid:138:7"
    assert sum(1 for row in compact for x in row if x["callback_data"]=="order:paid:138:7")==1
    assert sum(1 for row in expanded for x in row if x["callback_data"]=="order:paid:138:7")==1
    assert any(x["text"] == "⋯ Действия" for row in compact for x in row)
    if stage == "new":
        assert any(x["callback_data"].startswith("order:") and
                   x["callback_data"].split(":")[1] in ("sources","assembling","buyer","allocate")
                   for row in compact[1:] for x in row)
    if stage == "assembling":
        assert any(x["callback_data"]=="order:assembled:138:7" for row in compact for x in row)
    if stage == "shipped" and delivered == "dispatched":
        assert any(x["callback_data"]=="order:delivered:138:7" for row in compact for x in row)


@pytest.mark.parametrize("stage",["new","assembling","assembled","shipped"])
def test_paid_confirmation_disappears_but_fulfillment_buttons_remain(monkeypatch,stage):
    monkeypatch.setattr("app.services.hatuna_post_pick.post_pick_enabled_for_order",
                        lambda _id: True)
    current=order(stage,payment="paid",delivered="dispatched" if stage=="shipped" else "pending")
    primary=order_keyboard(current,[])["inline_keyboard"]
    expanded=available_order_keyboard(current,[])["inline_keyboard"]
    assert not any(x["callback_data"].split(":")[1]=="paid" for row in primary+expanded for x in row)
    if stage=="assembling":
        assert primary[0][0]["callback_data"]=="order:assembled:138:7"
    if stage=="shipped":
        assert primary[0][0]["callback_data"]=="order:delivered:138:7"


@pytest.mark.parametrize("stage",["new","assembling","assembled","shipped"])
def test_paid_writes_only_local_payment_state_does_not_touch_warehouse(stage):
    current=order(stage)
    current.status_changed_at=None
    current.status_changed_by_manager_id=None
    current.assembling_at=None
    current.assembled_at=None
    current.shipped_at=None
    current.paid_at=None
    current.paid_by_manager_id=None
    current.delivery_reference=None
    before=(current.fulfillment_status,current.delivery_status,current.delivery_method)
    instant=datetime(2026,10,25,3,0,tzinfo=timezone.utc)
    request=OrderAction(action="paid",expected_revision=7,
        idempotency_key="test-payment-first-138")
    apply_action(current,request,manager_id=23,now=instant)
    assert current.payment_status=="paid"
    assert current.paid_at==instant and current.paid_by_manager_id==23
    assert (current.fulfillment_status,current.delivery_status,current.delivery_method)==before
    assert current.revision==8


def test_canceled_orders_never_offer_payment():
    current=order("cancelled")
    buttons=str(order_keyboard(current,[],expanded=True))
    assert "order:paid:" not in buttons
