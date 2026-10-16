"""Manager post-pick menu is read-only after confirmed physical picking."""
from types import SimpleNamespace
import pytest

from app.bot.source_navigation import post_pick_followup_buttons, navigation


def plan(*, source="partner_x", status="awaiting_receipt"):
    alloc = SimpleNamespace(source_type=source, supply_status=status)
    return SimpleNamespace(allocations=[alloc])


def order(*, ident=8, stage="assembled", manual=True, review=False):
    return SimpleNamespace(id=ident, fulfillment_status=stage,
                           manual_fulfillment=manual, needs_review=review)


def callbacks(rows):
    return [entry["callback_data"] for row in rows for entry in row]


def recover_steps(rows):
    return [navigation[data.split(":", 1)[1]][1]["step"] for data in callbacks(rows)]


def test_assembled_tilda_order8_x_has_two_readonly_actions(monkeypatch):
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "true")
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ORDER_IDS", "8")
    navigation.clear()
    state = dict(actor=111, chat=-1001, order_id=8, revision=7)
    actions = post_pick_followup_buttons(order(), [plan(), plan(source="own")], state)
    assert len(actions) == 3
    assert recover_steps(actions) == ["post_pick_plan", "post_pick_check", "post_pick_ledger"]
    for row in actions:
        assert "без записи" in row[0]["text"].lower()
        assert "LIVE" not in row[0]["text"]
        token = row[0]["callback_data"].split(":")[1]
        action_state = navigation[token][1]
        assert action_state["actor"] == state["actor"]
        assert action_state["revision"] == state["revision"]


@pytest.mark.parametrize("stage", ["new", "assembling", "shipped", "cancelled"])
def test_not_assembled_cannot_see_post_pick_buttons(monkeypatch, stage):
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "true")
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ORDER_IDS", "8")
    assert post_pick_followup_buttons(order(stage=stage), [plan()], {"order_id": 8}) == []


@pytest.mark.parametrize("ident,manual,review,source,status", [
    (9, True, False, "partner_x", "awaiting_receipt"),
    (8, False, False, "partner_x", "awaiting_receipt"),
    (8, True, True, "partner_x", "awaiting_receipt"),
    (8, True, False, "own", "confirmed"),
    (8, True, False, "partner_x", "cancelled"),
])
def test_out_of_scope_restricted_order_never_offers_post_pick(monkeypatch, ident, manual, review, source, status):
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "true")
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ORDER_IDS", "8")
    assert post_pick_followup_buttons(
        order(ident=ident, manual=manual, review=review),
        [plan(source=source, status=status)], {"order_id": ident}) == []


def test_all_buttons_disabled_if_flag_off(monkeypatch):
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "false")
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ORDER_IDS", "8")
    assert not post_pick_followup_buttons(order(), [plan()], {"order_id": 8})
