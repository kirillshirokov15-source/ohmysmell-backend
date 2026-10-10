import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.database.base import Base
from app.integrations.tilda import normalize
from app.integrations.moysklad.async_gateway import ProductCatalogTTLCache, AsyncMoySkladGateway
from app.models.draft_order import DraftOrder
from app.models.order_desk import OrderDesk, DeskMessage, DeskEvent
from app.models.inbound_message import InboundMessage
from app.models.manager import Manager
from app.models.order import Order
from app.services import order_desk as service
from app.services.product_matching_service import ProductMatchingService
from app.services.tilda_review import review, source_rows
from tests.test_tilda_orders import tilda_config


def payload():
    return normalize({"tranid": "synthetic-38", "amount": "1680", "products": [
        {"externalid": "DSD020", "name": "Site name", "quantity": 1, "price": "1680"}]})


@pytest.fixture
def catalog(tilda_config, monkeypatch):
    rows = [{"id": "ms-dsd", "article": "DSD020", "code": "20", "name": "Catalog name",
             "salePrices": [{"value": 100}], "stock": 0}]
    mock = AsyncMock(return_value=rows)
    monkeypatch.setattr(ProductMatchingService, "products_async", mock)
    monkeypatch.setenv("TILDA_PRODUCT_MAP_JSON", "not even JSON")
    monkeypatch.delenv("MOYSKLAD_RETAIL_PRICE_TYPE", raising=False)
    return mock


@pytest.mark.parametrize("sku,products,status", [
    (" DSD020 ", [{"id": "a", "article": " DSD020 "}], "matched"),
    ("dsd020", [{"id": "a", "article": "DSD020"}], "not_found"),
    ("DSD020", [{"id": "a", "name": "DSD020"}], "not_found"),
    ("DSD020", [{"id": "a", "article": "DSD020", "code": "DSD020"}], "matched"),
    ("DSD020", [{"id": "a", "article": "DSD020"}, {"id": "b", "code": "DSD020"}], "ambiguous"),
    ("DSD020", [{"id": "a", "article": "DSD020", "archived": True}], "not_found"),
    ("unknown", [{"id": "a", "article": "DSD020"}], "not_found"),
])
def test_exact_sku(sku, products, status):
    result = ProductMatchingService.match_sku(products, sku, 1)
    assert result.status == status
    assert bool(result.product) == (status == "matched")


def test_declared_price_no_wholesale_zero_stock(catalog):
    rows, total, problems = asyncio.run(review(payload()))
    assert problems == [] and total == 168000
    assert rows[0]["price"] == rows[0]["item_total"] == 168000
    assert rows[0]["quantity_confidence"] == "confirmed"
    assert rows[0]["product_id"] == "ms-dsd"


@pytest.mark.parametrize("price", [None, "bad", "0", "-1", "1.001", True])
def test_invalid_price_is_reviewable(price, catalog):
    data = normalize({"tranid": "bad", "amount": "1680", "products": [
        {"externalid": "DSD020", "name": "A", "quantity": 1, "price": price}]})
    rows, total, problems = asyncio.run(review(data))
    assert rows[0]["price"] is None and total is None
    assert "invalid_reported_price" in problems


def test_expired_cache_failure_never_uses_old_products():
    clock = [0]
    cache = ProductCatalogTTLCache(ttl_seconds=10, clock=lambda: clock[0])
    cache.set([{"id": "a", "article": "DSD020"}])
    def fail():
        raise RuntimeError("offline")
    gateway = AsyncMoySkladGateway(SimpleNamespace(get_products=fail), cache)
    assert asyncio.run(gateway.get_products())[0]["id"] == "a"
    clock[0] = 10
    with pytest.raises(RuntimeError):
        asyncio.run(gateway.get_products())
    assert cache.get() is None


@pytest.fixture
def database(tmp_path, monkeypatch, catalog):
    # Real local persistence, no network or production database. PostgreSQL
    # concurrency/locks remain covered by opt-in staging tests.
    import app.models  # register metadata
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "tilda.db"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session, session.begin():
            session.add(Manager(telegram_id=111, name="Synthetic", is_active=True))
    asyncio.run(setup())
    monkeypatch.setattr(service, "async_session", factory)
    monkeypatch.setattr(service, "transaction_lock", AsyncMock())
    yield factory
    asyncio.run(engine.dispose())


async def get_draft(factory, did):
    async with factory() as s:
        return await s.scalar(select(DraftOrder).where(DraftOrder.id == did).options(selectinload(DraftOrder.items)))


@pytest.mark.parametrize("issue", ["total", "discount", "unknown_discount", "price", "missing", "ambiguous", "archived", "offline"])
def test_intake_and_confirm_fail_closed(database, catalog, issue):
    async def run():
        data = payload()
        if issue == "total": data["reported_total"] += 1
        if issue == "discount": data.update(discount=100, reported_total=167900)
        if issue == "unknown_discount": data["discount"] = None
        if issue == "price": data["items"][0]["price_minor"] = None
        if issue == "missing": catalog.return_value = []
        if issue == "ambiguous": catalog.return_value += [{"id": "b", "code": "DSD020"}]
        if issue == "archived": catalog.return_value[0]["archived"] = True
        if issue == "offline": catalog.side_effect = RuntimeError("offline")
        did = (await service.TildaIntake().submit(data))["draft_id"]
        draft = await get_draft(database, did)
        assert draft.total is None and draft.review_notes and draft.status == "needs_review"
        assert not draft.contact_details["tilda_review_approved"]
        await service.DeskService().claim(did, 111, -100123456)
        with pytest.raises(service.DeskError):
            await service.DeskService().confirm(did, 111, -100123456, draft.revision)
        async with database() as s:
            assert await s.scalar(select(func.count()).select_from(Order)) == 0
    asyncio.run(run())


def test_reprice_dry_run_binding_revision_no_duplicates(database, catalog):
    async def run():
        data = payload()
        data["items"].append({"externalid": "OTHER", "name": "Site name", "qty": 2, "price_minor": 500})
        data["reported_total"] += 1000
        catalog.return_value += [{"id": "ms-other", "code": "OTHER"}]
        did = (await service.TildaIntake().submit(data))["draft_id"]
        assert (await service.TildaIntake().submit(data))["duplicate"]
        await service.DeskService().claim(did, 111, -100123456)
        async with database() as s, s.begin():
            draft = await s.scalar(select(DraftOrder).where(DraftOrder.id == did).options(selectinload(DraftOrder.items)))
            draft.items[0].price = 1
            draft.contact_details = {**draft.contact_details, "tilda_review_approved": True}
            before_messages = await s.scalar(select(func.count()).select_from(DeskMessage))
            before_events = await s.scalar(select(func.count()).select_from(DeskEvent))
            desk = await s.get(OrderDesk, did)
            identity = (desk.external_id, desk.payload_hash)
        desk_service = service.DeskService()
        preview = await desk_service.reprice(did, 111, -100123456, dry_run=True)
        assert preview["changed"] and preview["revision"] == 0
        async with database() as s:
            assert await s.scalar(select(func.count()).select_from(DeskEvent)) == before_events
        assert (await get_draft(database, did)).items[0].price == 1
        assert (await desk_service.reprice(did, 111, -100123456))["revision"] == 1
        assert not (await desk_service.reprice(did, 111, -100123456))["changed"]
        draft = await get_draft(database, did)
        assert draft.total == 169000 and draft.revision == 1
        assert [(i.product_id, i.price) for i in sorted(draft.items, key=lambda i: i.id)] == [("ms-dsd",168000),("ms-other",500)]
        assert not draft.contact_details["tilda_review_approved"]
        with pytest.raises(service.DeskError, match="устарела"):
            await desk_service.confirm(did, 111, -100123456, 0)
        async with database() as s:
            desk = await s.get(OrderDesk, did)
            assert (desk.external_id, desk.payload_hash) == identity
            assert await s.scalar(select(func.count()).select_from(DeskMessage)) == before_messages
            assert await s.scalar(select(func.count()).select_from(DraftOrder)) == 1
            assert await s.scalar(select(func.count()).select_from(Order)) == 0
            inbound = await s.get(InboundMessage, draft.inbound_message_id)
            draft.items.reverse()
            _, ordered, _ = source_rows(draft, desk, inbound)
            assert ordered[0].product_id == "ms-dsd"
        async with database() as s, s.begin():
            (await s.get(DraftOrder, did)).status = "rejected"
        with pytest.raises(service.DeskError):
            await desk_service.reprice(did, 111, -100123456)
    asyncio.run(run())


def test_confirm_manager_approval_replay_and_shipment_source_guard(database, monkeypatch):
    from app.services.order_operations import OrderOperations, OrderAction, OperationError
    monkeypatch.setattr("app.services.order_operations.async_session", database)
    async def run():
        did = (await service.TildaIntake().submit(payload()))["draft_id"]
        await service.DeskService().claim(did, 111, -100123456)
        draft = await get_draft(database, did)
        assert draft.total == 168000 and not draft.contact_details["tilda_review_approved"]
        for actor, chat in [(222, -100123456), (111, -999)]:
            with pytest.raises(service.DeskError):
                await service.DeskService().confirm(did, actor, chat, 0)
        order = await service.DeskService().confirm(did, 111, -100123456, 0)
        assert order.total == 168000 and order.items[0].price == 168000
        assert (await get_draft(database, did)).contact_details["tilda_review_approved"]
        assert (await service.DeskService().confirm(did, 111, -100123456, 0)).id == order.id
        with pytest.raises(service.DeskError):
            await service.DeskService().reprice(did, 111, -100123456)
        async with database() as s, s.begin():
            saved = await s.get(Order, order.id)
            saved.fulfillment_status, saved.delivery_method = "assembled", "manual"
        with pytest.raises(OperationError, match="складам"):
            await OrderOperations().act(order.id, 111, OrderAction(action="shipped", expected_revision=0, idempotency_key="ship"))
        async with database() as s:
            assert (await s.get(Order, order.id)).fulfillment_status == "assembled"
            assert await s.scalar(select(func.count()).select_from(Order)) == 1
    asyncio.run(run())


@pytest.mark.parametrize("change", ["price", "item_total", "total", "quantity_confidence", "offline", "ambiguous"])
def test_confirmation_cannot_repair_or_hide_tampering(database, catalog, change):
    async def run():
        did = (await service.TildaIntake().submit(payload()))["draft_id"]
        await service.DeskService().claim(did, 111, -100123456)
        async with database() as s, s.begin():
            draft = await s.scalar(select(DraftOrder).where(DraftOrder.id == did).options(selectinload(DraftOrder.items)))
            if change == "total": draft.total += 1
            elif change in {"price", "item_total"}: setattr(draft.items[0], change, 100)
            elif change == "quantity_confidence": draft.items[0].quantity_confidence = "uncertain"
        if change == "offline": catalog.side_effect = RuntimeError("offline")
        if change == "ambiguous": catalog.return_value += [{"id": "b", "code": "DSD020"}]
        with pytest.raises(service.DeskError):
            await service.DeskService().confirm(did, 111, -100123456, 0)
        assert not (await get_draft(database, did)).contact_details["tilda_review_approved"]
    asyncio.run(run())


def test_legacy_binding_and_corrupt_source_rejected(database):
    async def run():
        did = (await service.TildaIntake().submit(payload()))["draft_id"]
        await service.DeskService().claim(did, 111, -100123456)
        async with database() as s, s.begin():
            draft = await s.get(DraftOrder, did)
            draft.contact_details = {k: v for k, v in draft.contact_details.items() if k != "tilda_item_sources"}
        assert (await service.DeskService().reprice(did, 111, -100123456))["changed"]
        async with database() as s, s.begin():
            draft = await s.get(DraftOrder, did)
            inbound = await s.get(InboundMessage, draft.inbound_message_id)
            altered = json.loads(inbound.body_text)
            altered["items"][0]["externalid"] = "OTHER"
            inbound.body_text = json.dumps(altered)
        with pytest.raises(service.DeskError):
            await service.DeskService().reprice(did, 111, -100123456)
    asyncio.run(run())


@pytest.mark.parametrize("amount", ["1679", "bad"])
def test_reported_line_arithmetic(amount, catalog):
    data = normalize({"tranid": "line-total", "amount": "1680", "products": [
        {"externalid": "DSD020", "quantity": 1, "price": "1680", "amount": amount}]})
    _, total, problems = asyncio.run(review(data))
    assert total is None and "item_total_mismatch" in problems


@pytest.mark.parametrize("stock,expected", [(0, 0), (3, 3), (None, None), ("NaN", None)])
def test_live_stock_hint_never_infers_missing_stock(monkeypatch, stock, expected):
    from app.services.tilda_availability import availability
    gateway = SimpleNamespace(get_stores=AsyncMock(return_value=[{"id": "store"}]),
        get_stock_by_store=AsyncMock(return_value={"rows": [{"meta": {"href": "/product/ms-dsd"},
            "stockByStore": [{"meta": {"href": "/store/store"}, "stock": stock, "reserve": 0}]}]}))
    monkeypatch.setattr("app.services.tilda_availability.ProductMatchingService",
                        lambda: SimpleNamespace(async_gateway=gateway))
    result = asyncio.run(availability([SimpleNamespace(product_id="ms-dsd")]))
    assert result.get("ms-dsd") == expected
