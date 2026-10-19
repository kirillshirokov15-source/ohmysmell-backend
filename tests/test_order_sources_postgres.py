import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tests.test_tilda_postgres import desk_db, staging_sessions, tilda_config
from tests.test_order_sources import full_scenario, seed_sources, CHAT
from app.models.manager import Manager
from app.services.product_matching_service import ProductMatchingService
from app.services.order_desk import DeskError
from app.schemas.order_sources import SourceAction
from sqlalchemy import select
from app.models.supply import ProductSupply
from app.repositories.order_repository import create_order
from app.services.fulfillment_service import FulfillmentService
from app.services.stock_allocation import StockAllocationError
from app.services import buying
from app.models.buying import BuyingPurchase

pytestmark = pytest.mark.staging


@pytest.fixture
def sources_postgres(desk_db, monkeypatch):
    factory, engine, actors = desk_db
    pid = "source-" + uuid4().hex
    monkeypatch.setattr(ProductMatchingService, "products_async",
        AsyncMock(return_value=[dict(id=pid, article="DSD020", name="Synthetic sources")]))
    async def seed():
        async with factory() as s, s.begin():
            s.add(Manager(telegram_id=actors[0], name="Synthetic source owner", is_active=True))
        await engine.dispose()
    asyncio.run(seed())
    return factory, engine, actors[0], pid


def test_split_sources_postgres_concurrency(sources_postgres):
    factory, engine, actor, pid = sources_postgres
    async def run():
        await full_scenario(factory, actor, pid=pid, concurrent=True)
        await engine.dispose()
    asyncio.run(run())


def test_two_orders_cannot_claim_same_available_stock(sources_postgres):
    factory, engine, actor, pid = sources_postgres
    async def run():
        first, service1, parts1 = await seed_sources(factory, actor, pid=pid)
        second, service2, parts2 = await seed_sources(factory, actor, pid=pid)
        for service in (service1, service2):
            service.products.get_catalog_async.return_value[0]["stocks"][0]["stock"] = 10
        async def claim(order, service, parts):
            return await service.act(order.id, actor, CHAT, SourceAction(action="assign",
                parts=[{**parts[0], "qty": 8}], expected_revision=0, idempotency_key="claim"))
        results = await asyncio.gather(claim(first, service1, parts1), claim(second, service2, parts2), return_exceptions=True)
        assert sum(isinstance(r, DeskError) for r in results) == 1
        assert sum(isinstance(r, dict) for r in results) == 1
        assigned = [(await s.read(o.id, actor, CHAT))["items"][0]["assigned"] for o, s in ((first, service1), (second, service2))]
        assert sorted(assigned) == [0, 8]
        await engine.dispose()
    asyncio.run(run())


def test_automatic_planner_honors_tilda_assignments(sources_postgres):
    factory, engine, actor, pid = sources_postgres
    async def run():
        order, service, parts = await seed_sources(factory, actor, pid=pid)
        service.products.get_catalog_async.return_value[0]["stocks"] = [dict(id="wh1", stock=10, reserve=0)]
        await service.act(order.id, actor, CHAT, SourceAction(action="assign", parts=[{**parts[0], "qty": 8}],
            expected_revision=0, idempotency_key="own"))
        # Synthetic catalog classification for the legacy automatic flow.
        async with factory() as s, s.begin():
            (await s.get(ProductSupply, pid)).source_type = "own"
        other = await create_order(dict(customer_name="Synthetic wholesale", phone="", source="email",
            customer_type="wholesale", total=400, items=[dict(id=pid,name="Synthetic",qty=4,price=100,sum=400)]))
        planner = FulfillmentService(service.products)
        with pytest.raises(StockAllocationError):
            await planner.plan(other.id)
        assert other.items[0].price == 100
        await engine.dispose()
    asyncio.run(run())


def test_buying_send_race_with_source_cancellation(sources_postgres):
    from fastapi import HTTPException
    factory, engine, actor, pid = sources_postgres
    async def run():
        order, service, parts = await seed_sources(factory, actor, pid=pid)
        view = await service.act(order.id, actor, CHAT, SourceAction(action="assign", parts=[parts[-1]], expected_revision=0, idempotency_key="external"))
        allocation = view["allocations"][0]
        async def send():
            async with factory() as s, s.begin():
                return await buying.simulate_send(s, allocation["purchase_id"], "synthetic")
        result = await asyncio.gather(send(), service.act(order.id, actor, CHAT,
            SourceAction(action="cancel", allocation_id=allocation["id"], expected_revision=1, idempotency_key="cancel")), return_exceptions=True)
        assert isinstance(result[1], dict)
        assert not isinstance(result[0], Exception) or isinstance(result[0], HTTPException)
        async with factory() as s:
            assert (await s.get(BuyingPurchase, allocation["purchase_id"])).status in {"sent", "cancelled"}
        assert (await service.read(order.id, actor, CHAT))["items"][0]["ready"] == 0
        await engine.dispose()
    asyncio.run(run())
