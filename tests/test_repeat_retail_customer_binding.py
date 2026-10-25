"""An existing retail MoySklad buyer should not force duplicate CRM customer creation.

Independent Tilda orders, real SQLite FKs; all MoySklad writes mocked away.
"""
import asyncio
from unittest.mock import AsyncMock
from sqlalchemy import select, func
import pytest

from tests.test_tilda_sku_review import database, catalog, tilda_config, payload
from app.models.order import Order
from app.models.draft_order import DraftOrder
from app.models.customer import Customer
from app.models.inbound_message import InboundMessage
from app.models.order_desk import DeskMessage
from app.services.order_desk import TildaIntake, DeskService, DeskError
from app.services.retail_buyer_binding import RetailBuyerService

CHAT=-100123456
MS_ID="cba5647e-89ec-40fa-951d-98056f8b5ff9"

async def orders(factory, *, second_phone="+7 (925) 354-44-50", second_email="repeat@example.test"):
    order_ids=[]
    for ix,(email,phone) in enumerate([
        ("repeat@example.test","+7 925 354 44 50"),
        (second_email,second_phone),
    ]):
        data=payload()
        data["external_id"]=f"synthetic-repeat-{ix}"
        data["email"]=email
        data["phone"]=phone
        draft_id=(await TildaIntake().submit(data))["draft_id"]
        await DeskService().claim(draft_id,111,CHAT)
        order=await DeskService().confirm(draft_id,111,CHAT,0)
        order_ids.append((order.id,draft_id))
    return order_ids


def test_repeated_retail_customer_reuses_prior_verified_owner_without_rewriting_orders(database,monkeypatch):
    from app.services import checkout_service
    monkeypatch.setattr(checkout_service, "transaction_lock", AsyncMock())

    async def run():
        first, second=await orders(database)
        srv=RetailBuyerService(matcher=object(),sessions=database)
        record={"id":MS_ID,"name":"Иван Петров","phone":"+7 925 354 44 50"}
        first_data=await srv._read(first[0],111,CHAT)
        assert "сопоставлен" in await srv._persist(first_data,111,CHAT,record,"phone")
        second_data=await srv._read(second[0],111,CHAT)
        assert second_data["customer_id"]!=first_data["customer_id"]
        async with database() as session:
            outbox=await session.scalar(select(func.count()).select_from(DeskMessage))
            old_revision=(await session.get(Order, first[0])).revision
        result=await srv._persist(second_data,111,CHAT,record,"phone")
        assert "повторная карточка" in result
        async with database() as session:
            old=await session.get(Order, first[0])
            new=await session.get(Order, second[0])
            draft=await session.get(DraftOrder,second[1])
            inbound=await session.get(InboundMessage,draft.inbound_message_id)
            assert old.revision==old_revision and old.counterparty_id==MS_ID
            assert old.customer_id==new.customer_id==first_data["customer_id"]
            assert draft.customer_id==inbound.customer_id==old.customer_id
            assert new.counterparty_id==MS_ID
            assert new.phone=="+7 (925) 354-44-50"
            assert await session.scalar(select(func.count()).select_from(Customer).where(
                Customer.moysklad_counterparty_id==MS_ID))==1
            assert await session.scalar(select(func.count()).select_from(DeskMessage))==outbox
        assert "уже привязан" in await srv._persist(
            second_data,111,CHAT,record,"phone")
    asyncio.run(run())


@pytest.mark.parametrize("phone,email",[
    ("+7 999 111 11 11","new@example.test"),
    ("",""),
])
def test_different_contact_can_not_take_previous_crm_customers_orders(database,monkeypatch,phone,email):
    from app.services import checkout_service
    monkeypatch.setattr(checkout_service,"transaction_lock",AsyncMock())
    async def run():
        first,second=await orders(database,second_phone=phone,second_email=email)
        srv=RetailBuyerService(matcher=object(),sessions=database)
        provider={"id":MS_ID,"name":"Иван Петров"}
        previous=await srv._read(first[0],111,CHAT)
        await srv._persist(previous,111,CHAT,provider,"phone")
        newcomer=await srv._read(second[0],111,CHAT)
        with pytest.raises(DeskError,match="не совпадают"):
            await srv._persist(newcomer,111,CHAT,provider,"phone")
        async with database() as s:
            curr=await s.get(Order,second[0])
            assert curr.customer_id==newcomer["customer_id"]
            assert curr.counterparty_id is None
    asyncio.run(run())


def test_matching_name_only_can_not_merge_into_another_customer(database,monkeypatch):
    from app.services import checkout_service
    monkeypatch.setattr(checkout_service,"transaction_lock",AsyncMock())
    async def run():
        first,second=await orders(database)
        srv=RetailBuyerService(matcher=object(),sessions=database)
        provider={"id":MS_ID,"name":"Иван Петров"}
        await srv._persist(await srv._read(first[0],111,CHAT),111,CHAT,provider,"full_name")
        with pytest.raises(DeskError,match="недостаточно совпадения имени"):
            await srv._persist(await srv._read(second[0],111,CHAT),111,CHAT,provider,"full_name")
    asyncio.run(run())
