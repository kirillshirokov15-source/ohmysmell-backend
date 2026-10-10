import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from tests.test_tilda_sku_review import database, catalog, tilda_config, payload
from app.config.settings import settings
from app.models.order import Order
from app.models.fulfillment import Shipment, WarehouseAllocation
from app.models.supply import Supplier, ProductSupply, SupplierOffer, XSettlement
from app.models.buying import BuyingSupplier, BuyingPurchase
from app.models.operations import OrderEvent
from app.schemas.order_sources import SourceAction
from app.services.order_source_service import OrderSourceService
from app.services.order_desk import TildaIntake, DeskService, DeskError
from app.services.order_operations import OrderOperations, OrderAction, OperationError
from app.services import buying

CHAT = -100123456


@pytest.fixture
def source_db(database, monkeypatch):
    for module in ("app.services.order_source_service", "app.services.order_operations", "app.services.manager_workspace"):
        monkeypatch.setattr(module + ".async_session", database)
    monkeypatch.setattr("app.services.order_source_service.transaction_lock", AsyncMock())
    monkeypatch.setattr(buying, "transaction_lock", AsyncMock())
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "external_writes_enabled", False)
    monkeypatch.setattr(settings, "warehouse_ids", ())
    return database


async def seed_sources(factory, actor=111, *, pid="ms-dsd", qty=8):
    data = payload()
    data["external_id"] = uuid4().hex
    data["items"][0]["qty"] = qty
    data["reported_total"] = 168000 * qty
    did = (await TildaIntake().submit(data))["draft_id"]
    await DeskService().claim(did, actor, CHAT)
    order = await DeskService().confirm(did, actor, CHAT, 0)
    async with factory() as s, s.begin():
        partner = Supplier(name="Synthetic X", supplier_type="partner_x")
        external = Supplier(name="Synthetic supplier", supplier_type="external_wholesaler", email="supplier@example.invalid")
        s.add_all([partner, external])
        await s.flush()
        if not await s.get(ProductSupply, pid):
            s.add(ProductSupply(product_id=pid, source_type="external"))
        s.add(BuyingSupplier(supplier_id=external.id, currency="RUB", parser={}))
        await s.flush()
        offer = SupplierOffer(product_id=pid, supplier_id=external.id, purchase_price_minor=90000,
                              currency_code="RUB", availability="confirmed", availability_qty=100)
        s.add(offer)
        await s.flush()
    products = SimpleNamespace(get_catalog_async=AsyncMock(return_value=[dict(id=pid, stocks=[
        dict(id="wh1", stock=20, reserve=0), dict(id="wh2", stock=20, reserve=0)])]))
    service = OrderSourceService(products)
    item_id = order.items[0].id
    parts = [dict(item_id=item_id, qty=2, source_type="own", warehouse_id="wh1"),
        dict(item_id=item_id, qty=1, source_type="own", warehouse_id="wh2"),
        dict(item_id=item_id, qty=1, source_type="partner_x", warehouse_id="wh1", supplier_id=partner.id, base_cost_minor=100000),
        dict(item_id=item_id, qty=1, source_type="partner_x", warehouse_id="wh2", supplier_id=partner.id, base_cost_minor=100000),
        dict(item_id=item_id, qty=3, source_type="external", warehouse_id="wh2", offer_id=offer.id)]
    return order, service, parts


async def full_scenario(factory, actor=111, *, pid="ms-dsd", concurrent=False):
    order, service, parts = await seed_sources(factory, actor, pid=pid)
    request = SourceAction(action="assign", parts=parts, expected_revision=0, idempotency_key=uuid4().hex)
    if concurrent:
        results = await asyncio.gather(*(service.act(order.id, actor, CHAT, request) for _ in range(3)))
        assert results[0] == results[1] == results[2]
        view = results[0]
    else:
        view = await service.act(order.id, actor, CHAT, request)
        assert await service.act(order.id, actor, CHAT, request) == view
    assert view["revision"] == 1 and len(view["allocations"]) == 5
    assert view["items"][0]["assigned"] == 8 and view["items"][0]["ready"] == 3
    external = next(a for a in view["allocations"] if a["source_type"] == "external")
    assert external["purchase_status"] == "draft" and external["requires_purchase"]
    with pytest.raises(DeskError, match="устарела"):
        await service.act(order.id, actor, CHAT, request.model_copy(update={"idempotency_key": uuid4().hex}))
    async def source_action(action, allocation):
        nonlocal view
        req = SourceAction(action=action, allocation_id=allocation["id"],
            expected_revision=view["revision"], idempotency_key=uuid4().hex)
        view = await service.act(order.id, actor, CHAT, req)
        assert await service.act(order.id, actor, CHAT, req) == view
    with pytest.raises(DeskError, match="Buying"):
        await source_action("receive", external)
    with pytest.raises(OperationError):
        await OrderOperations().act(order.id, actor, OrderAction(action="assembling", expected_revision=1, idempotency_key=uuid4().hex))
    async with factory() as s, s.begin():
        purchase = await buying.simulate_send(s, external["purchase_id"], "synthetic")
        assert purchase.status == "sent"
    with pytest.raises(DeskError, match="Buying"):
        await source_action("receive", external)
    async with factory() as s, s.begin():
        await buying.receive(s, external["purchase_id"], "synthetic")
        await buying.receive(s, external["purchase_id"], "synthetic")
    for allocation in list(view["allocations"]):
        if allocation["source_type"] == "own":
            continue
        with pytest.raises(DeskError, match="предыдущий"):
            await source_action("post_receipt", allocation)
        await source_action("receive", allocation)
        assert not next(a for a in view["allocations"] if a["id"] == allocation["id"])["ready"]
        await source_action("post_receipt", allocation)
    assert view["items"][0]["ready"] == 8
    from app.services.manager_workspace import order_card, shipments_for
    plans = await shipments_for(order.id)
    text = order_card(order, plans)
    assert all(label in text for label in ("Наш склад", "X-склад", "Внешние поставщики", "Buying", "симуляция"))
    revision = view["revision"]
    for action in ("assembling", "assembled", "delivery"):
        order = await OrderOperations().act(order.id, actor, OrderAction(action=action, expected_revision=revision,
            idempotency_key=uuid4().hex, **({"delivery_method": "manual"} if action == "delivery" else {})))
        revision = order.revision
    async with factory() as s, s.begin():
        (await s.get(Shipment, plans[0].id)).status = "cancelled"
    with pytest.raises(OperationError):
        await OrderOperations().act(order.id, actor, OrderAction(action="shipped", expected_revision=revision, idempotency_key=uuid4().hex))
    async with factory() as s, s.begin():
        assert (await s.get(Order, order.id)).fulfillment_status == "assembled"
        (await s.get(Shipment, plans[0].id)).status = "planned"
    order = await OrderOperations().act(order.id, actor, OrderAction(action="shipped", expected_revision=revision, idempotency_key=uuid4().hex))
    assert order.fulfillment_status == "shipped" and order.total == 8 * 168000
    assert order.items[0].price == 168000 and order.items[0].qty == 8
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(XSettlement).where(XSettlement.order_id == order.id)) == 2
        assert await s.scalar(select(func.count()).select_from(OrderEvent).where(OrderEvent.order_id == order.id, OrderEvent.action == "source_assign")) == 1
        assert (await s.get(BuyingPurchase, external["purchase_id"])).snapshot["total_minor"] == 270000


def test_split_sources_full_lifecycle(source_db):
    asyncio.run(full_scenario(source_db))


def test_atomic_overallocation_cancel_and_reassign(source_db):
    async def run():
        order, service, parts = await seed_sources(source_db)
        with pytest.raises(DeskError):
            await service.act(order.id, 222, CHAT, SourceAction(action="assign", parts=parts, expected_revision=0, idempotency_key="denied"))
        bad = [{**parts[0], "qty": 9}]
        with pytest.raises(DeskError, match="превышает"):
            await service.act(order.id, 111, CHAT, SourceAction(action="assign", parts=bad, expected_revision=0, idempotency_key="over"))
        view = await service.act(order.id, 111, CHAT, SourceAction(action="assign", parts=[parts[0]], expected_revision=0, idempotency_key="first"))
        allocation = view["allocations"][0]
        view = await service.act(order.id, 111, CHAT, SourceAction(action="cancel", allocation_id=allocation["id"], expected_revision=1, idempotency_key="cancel"))
        assert view["items"][0]["assigned"] == 0
        view = await service.act(order.id, 111, CHAT, SourceAction(action="assign", parts=[parts[0]], expected_revision=2, idempotency_key="again"))
        assert view["items"][0]["assigned"] == 2 and len(view["allocations"]) == 2
        assert view["allocations"][0]["status"] == "cancelled"
    asyncio.run(run())


@pytest.mark.parametrize("sent", [False, True])
def test_cancel_external_allocation_does_not_fake_purchase_receipt(source_db, sent):
    async def run():
        order, service, parts = await seed_sources(source_db)
        view = await service.act(order.id, 111, CHAT, SourceAction(action="assign", parts=[parts[-1]], expected_revision=0, idempotency_key="external"))
        allocation = view["allocations"][0]
        if sent:
            async with source_db() as s, s.begin():
                await buying.simulate_send(s, allocation["purchase_id"], "synthetic")
        view = await service.act(order.id, 111, CHAT, SourceAction(action="cancel", allocation_id=allocation["id"], expected_revision=1, idempotency_key="cancel"))
        assert view["items"][0]["ready"] == view["items"][0]["assigned"] == 0
        assert view["allocations"][0]["purchase_status"] == ("sent" if sent else "cancelled")
        assert not view["allocations"][0]["receipt_reference"]
    asyncio.run(run())


def test_cancel_order_closes_its_sources_and_unsent_buying(source_db):
    async def run():
        order, service, parts = await seed_sources(source_db)
        view = await service.act(order.id, 111, CHAT, SourceAction(action="assign", parts=parts, expected_revision=0, idempotency_key="all"))
        await OrderOperations().act(order.id, 111, OrderAction(action="cancel", expected_revision=1, idempotency_key="cancel-order"))
        view = await service.read(order.id, 111, CHAT)
        assert view["items"][0]["ready"] == view["items"][0]["assigned"] == 0
        assert all(a["status"] == a["plan_status"] == "cancelled" for a in view["allocations"])
        assert next(a for a in view["allocations"] if a["source_type"] == "external")["purchase_status"] == "cancelled"
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["stock", "offline", "warehouse", "production"])
def test_fail_closed(source_db, monkeypatch, failure):
    async def run():
        order, service, parts = await seed_sources(source_db)
        selected = [parts[0]]
        if failure == "stock": service.products.get_catalog_async.return_value[0]["stocks"][0]["stock"] = 0
        if failure == "offline": service.products.get_catalog_async.side_effect = RuntimeError("offline")
        if failure == "warehouse": selected[0]["warehouse_id"] = "missing"
        if failure == "production":
            view = await service.act(order.id, 111, CHAT, SourceAction(action="assign", parts=[parts[2]], expected_revision=0, idempotency_key="x"))
            monkeypatch.setattr(settings, "environment", "production")
            with pytest.raises(DeskError, match="симуляции"):
                await service.act(order.id, 111, CHAT, SourceAction(action="receive", allocation_id=view["allocations"][0]["id"], expected_revision=1, idempotency_key="receive"))
            return
        with pytest.raises(DeskError):
            await service.act(order.id, 111, CHAT, SourceAction(action="assign", parts=selected, expected_revision=0, idempotency_key="bad"))
        view = await service.read(order.id, 111, CHAT)
        assert view["revision"] == 0 and view["allocations"] == []
    asyncio.run(run())


def test_stale_cancelled_and_uncertain_plans_never_ready(source_db, monkeypatch):
    from app.services.order_source_service import allocation_ready
    async def run():
        order, service, parts = await seed_sources(source_db)
        view = await service.act(order.id, 111, CHAT, SourceAction(action="assign", parts=[parts[0]], expected_revision=0, idempotency_key="own"))
        async with source_db() as s, s.begin():
            plan = await s.scalar(select(Shipment).where(Shipment.order_id == order.id))
            plan.status = "uncertain"
        view = await service.read(order.id, 111, CHAT)
        assert view["items"][0]["ready"] == 0
        a = SimpleNamespace(managed_source=True, source_type="partner_x", supply_status="confirmed",
            receipt_reference="simulated:1", receipt_simulated=True)
        assert allocation_ready(a, manual=True)
        monkeypatch.setattr(settings, "environment", "production")
        assert not allocation_ready(a, manual=True)
    asyncio.run(run())


def test_source_api_requires_token_and_owner(source_db, monkeypatch):
    from fastapi import FastAPI
    from app.api import supply
    from tests.asgi_client import request
    monkeypatch.setattr(supply, "async_session", source_db)
    monkeypatch.setattr(settings, "internal_api_token", "synthetic-source-token")
    app = FastAPI()
    app.include_router(supply.router)
    async def run():
        order, _, _ = await seed_sources(source_db)
        path = f"/internal/supply/orders/{order.id}/sources"
        assert (await request(app, "GET", path))[0] == 401
        headers = {"X-Internal-API-Token": "synthetic-source-token", "X-Manager-Telegram-Id": "111"}
        status, body, _ = await request(app, "GET", path, headers=headers)
        assert status == 200 and body["order_id"] == order.id
    asyncio.run(run())


def test_telegram_assignment_is_versioned_and_idempotent(monkeypatch):
    from app.bot.order_sources import register
    handlers = []
    class Observer:
        def __call__(self, *args):
            def decorator(function):
                handlers.append(function)
                return function
            return decorator
    service = SimpleNamespace(act=AsyncMock(), read=AsyncMock(return_value=dict(order_id=10, revision=5, items=[], allocations=[])))
    monkeypatch.setattr("app.bot.order_sources.OrderSourceService", lambda: service)
    register(SimpleNamespace(message=Observer()), AsyncMock(return_value=True))
    message = SimpleNamespace(text="/sourceadd 10 4 20 2 own wh1", from_user=SimpleNamespace(id=111),
        chat=SimpleNamespace(id=CHAT), message_id=70, answer=AsyncMock())
    asyncio.run(handlers[0](message))
    args = service.act.await_args.args
    assert args[:3] == (10, 111, CHAT)
    assert args[3].expected_revision == 4 and args[3].parts[0].qty == 2
    assert args[3].idempotency_key == f"source:{CHAT}:70"
