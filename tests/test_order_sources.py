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


def test_one_purchase_supplies_multiple_orders(source_db):
    async def run():
        first,service,parts=await seed_sources(source_db)
        second,_,_=await seed_sources(source_db)
        item=(parts[-1])
        async with source_db() as s,s.begin():
            offer=await s.get(SupplierOffer,item['offer_id'])
            purchase=await buying.create_purchase(s,dict(supplier_id=offer.supplier_id,supplier_name='Synthetic',
                recipient='supplier@example.invalid',currency='RUB',total_minor=540000,approximate=False,
                approximate_rub_minor=540000,email_body='Item 6',items=[dict(offer_id=offer.id,product_id='ms-dsd',
                name='Item',quantity=6,unit_price_minor=90000)]),'test')
        views=[]
        for order in (first,second):
            part={**item,'item_id':order.items[0].id,'purchase_id':purchase.id}
            views.append(await service.act(order.id,111,CHAT,SourceAction(action='assign',parts=[part],
                expected_revision=0,idempotency_key=uuid4().hex)))
        for index,order in enumerate((first,second)):
            allocation=views[index]['allocations'][0]
            await service.act(order.id,111,CHAT,SourceAction(action='cancel',allocation_id=allocation['id'],
                expected_revision=1,idempotency_key=uuid4().hex))
            async with source_db() as s:
                assert (await s.get(BuyingPurchase,purchase.id)).status==('draft' if index==0 else 'cancelled')
    asyncio.run(run())


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
    from app.services.manager_workspace import order_details_card as order_card, shipments_for
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


def test_split_sources_full_lifecycle(source_db, monkeypatch):
    """One Tilda order: only OWN and X. Buying is never inferred from X."""
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "true")
    async def run():
        order, service, parts = await seed_sources(source_db)
        own = {**parts[0], "qty": 2}
        x = {key: val for key,val in parts[3].items()
             if key not in {"supplier_id", "base_cost_minor"}}
        x["qty"] = 6
        request = SourceAction(action="assign", parts=[own, x],
            expected_revision=0, idempotency_key=uuid4().hex)
        view = await service.act(order.id, 111, CHAT, request)
        assert view == await service.act(order.id, 111, CHAT, request)
        assert view["items"][0]["assigned"] == order.items[0].qty == 8
        assert view["items"][0]["ready"] == 2
        assert {a["source_type"] for a in view["allocations"]} == {"own", "partner_x"}
        assert {a["plan_status"] for a in view["allocations"]} == {"planned"}
        assert all(a.get("purchase_id") is None for a in view["allocations"])
        async with source_db() as session:
            assert await session.scalar(select(func.count()).select_from(XSettlement)
                .where(XSettlement.order_id == order.id)) == 0
            assert (await session.get(Order, order.id)).total == 8 * 168000
    asyncio.run(run())




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


def test_cancel_order_closes_own_and_x_sources(source_db, monkeypatch):
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "true")
    async def run():
        order, service, parts = await seed_sources(source_db)
        x = {key: val for key,val in parts[3].items()
             if key not in {"supplier_id", "base_cost_minor"}}
        x["qty"] = 6
        view = await service.act(order.id, 111, CHAT, SourceAction(
            action="assign", parts=[parts[0], x], expected_revision=0, idempotency_key="all"))
        await OrderOperations().act(order.id, 111,
            OrderAction(action="cancel", expected_revision=view["revision"], idempotency_key="cancel-order"))
        state = await service.read(order.id, 111, CHAT)
        assert state["items"][0]["ready"] == state["items"][0]["assigned"] == 0
        assert all(a["status"] == a["plan_status"] == "cancelled" for a in state["allocations"])
        assert not any(a.get("purchase_id") for a in state["allocations"])
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
            clean_x = {k:v for k,v in parts[2].items() if k not in {"supplier_id", "base_cost_minor"}}
            view = await service.act(order.id, 111, CHAT, SourceAction(action="assign", parts=[clean_x], expected_revision=0, idempotency_key="x"))
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
    asyncio.run(next(h for h in handlers if h.__name__ == 'sources')(message))
    args = service.act.await_args.args
    assert args[:3] == (10, 111, CHAT)
    assert args[3].expected_revision == 4 and args[3].parts[0].qty == 2
    assert args[3].idempotency_key == f"source:{CHAT}:70"


def test_x_allocation_does_not_create_supplier_or_settlement(source_db):
    async def run():
        order, service, parts = await seed_sources(source_db)
        clean = [{key:value for key,value in row.items()
                  if key not in {"supplier_id", "base_cost_minor"}}
                 for row in parts[2:4]]
        async with source_db() as session:
            before = await session.scalar(select(func.count()).select_from(Supplier)
                .where(Supplier.supplier_type == "partner_x"))
        view = await service.act(order.id,111,CHAT,SourceAction(
            action="assign",parts=clean,expected_revision=0,idempotency_key=uuid4().hex))
        assert view["items"][0]["assigned"] == 2
        assert all(a["supplier_id"] is None for a in view["allocations"])
        async with source_db() as session:
            after = await session.scalar(select(func.count()).select_from(Supplier)
                .where(Supplier.supplier_type == "partner_x"))
            settlements = await session.scalar(select(func.count()).select_from(XSettlement)
                .where(XSettlement.order_id == order.id))
        assert (before,after,settlements) == (1,1,0)
    asyncio.run(run())




def test_multiple_legacy_x_partners_do_not_affect_own_x_allocation(source_db, monkeypatch):
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "true")
    async def run():
        order, service, parts = await seed_sources(source_db)
        async with source_db() as session,session.begin():
            session.add(Supplier(name="Old X agent not required",supplier_type="partner_x"))
        clean = {key:value for key,value in parts[2].items()
                 if key not in {"supplier_id", "base_cost_minor"}}
        view = await service.act(order.id,111,CHAT,SourceAction(
            action="assign",parts=[clean],expected_revision=0,idempotency_key=uuid4().hex))
        assert view["items"][0]["assigned"] == 1
        assert view["allocations"][0]["supplier_id"] is None
    asyncio.run(run())




def test_x_sale_price_immutable_and_manual_receipts_disabled(source_db, monkeypatch):
    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "true")
    async def run():
        order, service, parts = await seed_sources(source_db)
        clean = {key:value for key,value in parts[2].items()
                 if key not in {"supplier_id", "base_cost_minor"}}
        view = await service.act(order.id,111,CHAT,SourceAction(
            action="assign",parts=[clean],expected_revision=0,idempotency_key=uuid4().hex))
        alloc = view["allocations"][0]
        assert alloc["source_type"] == "partner_x"
        assert alloc["supplier_id"] is None
        assert view["items"][0]["sale_unit_minor"] == 168000
        for action in ("receive","post_receipt"):
            with pytest.raises(DeskError, match="автоматически"):
                await service.act(order.id,111,CHAT,SourceAction(
                    action=action,allocation_id=alloc["id"],
                    expected_revision=view["revision"],idempotency_key=uuid4().hex))
        with pytest.raises(DeskError, match="автоматически"):
            await service.act(order.id,111,CHAT,SourceAction(
                action="set_x_cost",allocation_id=alloc["id"],x_unit_cost_minor=50000,
                expected_revision=view["revision"],idempotency_key=uuid4().hex))
        async with source_db() as session:
            o = await session.scalar(select(Order).where(Order.id == order.id)
                .options(selectinload(Order.items)))
            assert o.items[0].price == 168000
            assert o.total == 8*168000
            assert await session.scalar(select(func.count()).select_from(XSettlement)) == 0
    asyncio.run(run())




def test_x_cost_cannot_be_substituted_from_tilda_retail_or_reassigned(source_db):
    async def run():
        order, service, parts = await seed_sources(source_db)
        x_part = {key: value for key, value in parts[2].items()
                  if key not in {"supplier_id", "base_cost_minor"}}
        view = await service.act(order.id, 111, CHAT, SourceAction(
            action="assign", parts=[x_part], expected_revision=0,
            idempotency_key=uuid4().hex))
        allocation = view["allocations"][0]
        # Manager cannot use invoice price as the purchase cost implicitly;
        # a separate explicit cost action and non-negative margin are required.
        with pytest.raises(DeskError, match="финансовая"):
            await service.act(order.id, 111, CHAT, SourceAction(
                action="set_x_cost", allocation_id=allocation["id"],
                x_unit_cost_minor=200000, expected_revision=1,
                idempotency_key=uuid4().hex))
        stale = await service.read(order.id, 111, CHAT)
        assert stale["revision"] == 1 and stale["allocations"][0]["x_cost_pending"]
        with pytest.raises(ValueError):
            SourceAction(action="set_x_cost", allocation_id=allocation["id"],
                expected_revision=1, idempotency_key=uuid4().hex)
    asyncio.run(run())
