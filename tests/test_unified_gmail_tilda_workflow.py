"""Unified new Gmail/Tilda order workflow with no Gmail delivery, no payment gates.

Every database test runs against local SQLite. No provider writes or mail sends.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as Obj
from sqlalchemy import select
from sqlalchemy.orm import selectinload
import pytest

from tests.test_tilda_sku_review import database, catalog, tilda_config
from app.models.customer import Customer
from app.models.inbound_message import InboundMessage
from app.models.draft_order import DraftOrder, DraftOrderItem
from app.models.order import Order
from app.models.order_desk import OrderDesk
from app.models.sales import CustomerType
from app.repositories.draft_order_repository import DraftOrderRepository
from app.services.manager_workspace import order_keyboard, available_order_keyboard, order_details_card
from app.services.order_operations import OrderAction, OperationError, apply_action
from app.services.desk_status import stage_label


def sample_order(source, status="new", payment="unpaid"):
    return Obj(
        id=444,revision=8,status="new",source=source,customer_name="Buyer",
        customer_type="wholesale" if source=="email" else "retail",
        customer_email="buyer@example.org", phone="", telegram="",
        payment_status=payment, fulfillment_status=status,
        delivery_method="unselected",delivery_status="pending",
        delivery_reference=None,needs_review=False,manual_fulfillment=True,
        counterparty_id="verified-provider",counterparty_name="Buyer",
        items=[Obj(id=4,name="Item",qty=1,price=200000,item_total=200000)],
        total=200000, ui_supply_ready=True, ui_cancellable=True,
        desk_draft_id=33,paid_at=None,paid_by_manager_id=None,payment_note=None,
        assigned_manager_telegram_id=None,comment=None, status_changed_at=None,
        status_changed_by_manager_id=None,
    )


async def make_email_ready(factory):
    async with factory() as session,session.begin():
        customer=Customer(customer_type=CustomerType.WHOLESALE, display_name="Test Wholesale")
        session.add(customer); await session.flush()
        inbound=InboundMessage(source="email",external_message_id="unified-synthetic-gmail-001",
            sender="buyer@example.org",subject="Заказ",body_text="SKU 1 шт",
            received_at=datetime.now(timezone.utc),customer_id=customer.id)
        session.add(inbound);await session.flush()
        draft=DraftOrder(source="email",status="ready",revision=0,
            inbound_message_id=inbound.id,customer_id=customer.id,
            customer_type=CustomerType.WHOLESALE,sender_email="buyer@example.org",
            customer_name="Test Wholesale",subject="Заказ",
            contact_details={"mail_intent":"order"},counterparty_id="provider-buyer-123",
            counterparty_name="ООО Покупатель",total=200000)
        draft.items.append(DraftOrderItem(raw_product_text="SKU",qty=1,
            match_status="matched",product_id="product-123",
            product_name="Product",article="SKU",price=200000,item_total=200000))
        session.add(draft);await session.flush()
        return draft.id


def test_manager_confirmed_gmail_uses_one_order_desk_and_source_workflow(database,monkeypatch):
    monkeypatch.setattr("app.repositories.draft_order_repository.async_session",database)
    repo=DraftOrderRepository()
    async def run():
        draft_id=await make_email_ready(database)
        order=await repo.finalize(draft_id,manager_telegram_id=111)
        assert order.source=="email" and order.manual_fulfillment is True
        assert order.counterparty_id=="provider-buyer-123" and order.total==200000
        async with database() as s:
            desk=await s.get(OrderDesk,draft_id)
            assert desk.order_id==order.id and desk.actor_telegram_id==111
            assert desk.customer_telegram_id is None
            assert desk.external_id==f"gmail:confirmed:{draft_id}"
        repeated=await repo.finalize(draft_id,manager_telegram_id=111)
        assert repeated.id==order.id
        async with database() as s:
            assert len((await s.scalars(select(Order).where(Order.source=="email"))).all())==1
            assert len((await s.scalars(select(OrderDesk).where(OrderDesk.order_id==order.id))).all())==1
    asyncio.run(run())


def test_legacy_wholesale_finalize_keeps_legacy_accounting(database,monkeypatch):
    monkeypatch.setattr("app.repositories.draft_order_repository.async_session",database)
    async def run():
        draft_id=await make_email_ready(database)
        order=await DraftOrderRepository().finalize(draft_id)
        assert not order.manual_fulfillment
        async with database() as s:
            assert await s.get(OrderDesk,draft_id) is None
    asyncio.run(run())


@pytest.mark.parametrize("source",["website","email"])
def test_manual_payment_does_not_change_shipment_delivery_or_status(source):
    order=sample_order(source,status="assembling")
    before=(order.fulfillment_status,order.delivery_method,
            order.delivery_status,order.status_changed_at)
    apply_action(order,OrderAction(action="paid",expected_revision=8,
                 idempotency_key="test-paid-01"),13,datetime.now(timezone.utc))
    assert order.payment_status=="paid"
    assert (order.fulfillment_status,order.delivery_method,
            order.delivery_status,order.status_changed_at)==before


@pytest.mark.parametrize("source",["website","email"])
def test_paid_status_does_not_block_cancel_before_shipment(source):
    order=sample_order(source,payment="paid")
    apply_action(order,OrderAction(action="cancel",expected_revision=8,
        idempotency_key="test-cancel-01"),13,datetime.now(timezone.utc))
    assert order.fulfillment_status=="cancelled"


def test_gmail_shipment_has_no_delivery_gate_or_delivery_buttons():
    order=sample_order("email",status="assembled")
    menu=order_keyboard(order,[],expanded=True)["inline_keyboard"]
    actions=[b["callback_data"].split(":")[1] for row in menu for b in row]
    assert "shipped" in actions
    assert "delivered" not in actions
    assert all(not action.startswith("delivery_") for action in actions)
    apply_action(order,OrderAction(action="shipped",expected_revision=8,
        idempotency_key="test-gmail-ship"),13,datetime.now(timezone.utc))
    assert order.fulfillment_status=="shipped"
    assert order.delivery_status=="pending"
    assert "Доставк" not in order_details_card(order)


def test_tilda_retains_delivery_selection_and_delivered_transition():
    order=sample_order("website",status="assembled")
    actions=str(order_keyboard(order,[],expanded=True))
    assert "delivery_pickup" in actions
    assert "Отгружен" not in actions
    with pytest.raises(OperationError,match="способ доставки"):
        apply_action(order,OrderAction(action="shipped",expected_revision=8,
            idempotency_key="test-site-gate"),13,datetime.now(timezone.utc))
    apply_action(order,OrderAction(action="delivery",expected_revision=8,
        idempotency_key="test-site-method",delivery_method="pickup"),13,datetime.now(timezone.utc))
    apply_action(order,OrderAction(action="shipped",expected_revision=9,
        idempotency_key="test-site-ship"),13,datetime.now(timezone.utc))
    assert order.delivery_status=="dispatched"
    assert "delivered" in str(order_keyboard(order,[],expanded=True))
    apply_action(order,OrderAction(action="delivery",expected_revision=10,
        idempotency_key="test-site-delivered",delivery_status="delivered"),13,
        datetime.now(timezone.utc))
    assert stage_label(None,None,order)=="Доставлен"


def test_email_delivery_action_rejected():
    order=sample_order("email")
    with pytest.raises(OperationError,match="только для заказов Tilda"):
        apply_action(order,OrderAction(action="delivery",expected_revision=8,
            idempotency_key="test-gmail-delivery",delivery_method="pickup"),13,
            datetime.now(timezone.utc))
