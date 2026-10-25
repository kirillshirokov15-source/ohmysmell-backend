"""SQLite integration: binding an existing buyer updates order, draft and customer together,
with no outbound messages, provider writes or retroactive shipment.
"""
import asyncio
from sqlalchemy import select, func
from unittest.mock import AsyncMock

from tests.test_tilda_sku_review import database, catalog, tilda_config, payload
from app.services.order_desk import TildaIntake, DeskService
from app.services.retail_buyer_binding import RetailBuyerService
from app.models.order import Order
from app.models.customer import Customer
from app.models.draft_order import DraftOrder
from app.models.order_desk import OrderDesk, DeskMessage
from app.models.operations import OrderEvent

CHAT = -100123456

def test_real_sqlite_tilda_retail_buyer_binding_is_atomic_and_silent(database, monkeypatch):
    from app.services import checkout_service as locks
    monkeypatch.setattr(locks, "transaction_lock", AsyncMock())
    async def run():
        did = (await TildaIntake().submit(payload()))["draft_id"]
        await DeskService().claim(did, 111, CHAT)
        order = await DeskService().confirm(did, 111, CHAT, 0)
        async with database() as session:
            before = await session.scalar(select(func.count()).select_from(DeskMessage))
            orig = dict(order_id=order.id, revision=order.revision)
        service = RetailBuyerService(matcher=object(), sessions=database)
        record = {"id":"cba5647e-89ec-40fa-951d-98056f8b5ff9",
                  "name":"Иван Петров", "archived":False}
        assert "сопоставлен" in (await service._persist(orig,111,CHAT,record,"manager_created"))
        async with database() as session:
            linked = await session.get(Order,order.id)
            draft = await session.get(DraftOrder,did)
            cust = await session.get(Customer, linked.customer_id)
            assert linked.revision == orig["revision"]+1
            assert linked.counterparty_id == record["id"]
            assert draft.counterparty_id == record["id"]
            assert cust.moysklad_counterparty_id == record["id"]
            assert await session.scalar(select(func.count()).select_from(DeskMessage)) == before
            assert await session.scalar(select(func.count()).select_from(OrderEvent).where(
                OrderEvent.order_id==order.id,OrderEvent.action=="buyer_bound")) == 1
        assert "уже привязан" in (await service._persist(orig,111,CHAT,record,"manager_created"))
        async with database() as session:
            assert await session.scalar(select(func.count()).select_from(DeskMessage)) == before
    asyncio.run(run())
