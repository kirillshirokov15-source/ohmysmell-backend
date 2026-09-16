"""Opt in through scripts.run_staging_tests; no production connection fallback."""
import asyncio
import importlib
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

pytestmark = pytest.mark.staging


@pytest.fixture
def staging_sessions(monkeypatch):
    if os.getenv("OMS_STAGING_TESTS") != "1":
        pytest.skip("Explicit isolated staging test run required")
    from app.bot.staging_runner import validate_staging_config
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.config.settings import settings
    config = validate_staging_config(dict(os.environ))
    schema = os.environ.get("OMS_STAGING_SCHEMA", "")
    import re
    assert re.fullmatch(r"oms_audit_[a-f0-9]{32}", schema), "Only isolated audit schemas allowed"
    url = make_url(config.database_url)
    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    connect_args = {"server_settings": {"search_path": schema, "statement_timeout": "30000"}, "timeout": 10}
    if sslmode:
        connect_args["ssl"] = sslmode != "disable"
    engine = create_async_engine(url.set(drivername="postgresql+asyncpg", query=query),
                                 pool_size=5, max_overflow=5, connect_args=connect_args)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for module in ("app.repositories.customer_repository", "app.repositories.draft_order_repository",
                   "app.repositories.order_repository", "app.repositories.email_cursor_repository",
                   "app.services.checkout_service", "app.services.fulfillment_service",
                   "app.services.external_operation_service", "app.workers.notifications",
                   "app.services.order_operations", "app.services.client_channel", "app.services.manager_workspace"):
        monkeypatch.setattr(importlib.import_module(module), "async_session", factory)
    monkeypatch.setattr(settings, "external_writes_enabled", False)
    monkeypatch.setattr(settings, "warehouse_ids", ())
    return factory, engine


async def ready_draft():
    from datetime import datetime, timezone
    from app.repositories.customer_repository import CustomerRepository
    from app.repositories.draft_order_repository import DraftOrderRepository
    from app.models.sales import CustomerType
    uid = uuid4().hex
    customer = await CustomerRepository().create(CustomerType.WHOLESALE, "STAGING AUDIT " + uid, [])
    return await DraftOrderRepository().create({"external_message_id": "audit:" + uid,
        "sender_email": uid + "@example.invalid", "body_text": "Product x4",
        "received_at": datetime.now(timezone.utc), "customer_id": customer.id,
        "customer_type": "wholesale", "counterparty_id": "cp-" + uid,
        "status": "ready", "total": 39996, "items": [{"raw_product_text": "Product",
        "qty": 4, "match_status": "matched", "product_id": "p1", "product_name": "Product",
        "article": "P1", "price": 9999, "item_total": 39996, "candidates": []}]})


def test_concurrent_finalize_creates_one_order(staging_sessions):
    from app.repositories.draft_order_repository import DraftOrderRepository
    from sqlalchemy import select, func
    from app.models.order import Order
    factory, engine = staging_sessions
    async def run():
        draft = await ready_draft()
        orders = await asyncio.gather(*(DraftOrderRepository().finalize(draft.id) for _ in range(8)))
        assert len({o.id for o in orders}) == 1
        assert all(o.items[0].price == 9999 and o.total == 39996 and o.moysklad_order_id is None for o in orders)
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(Order).where(Order.customer_id == draft.customer_id)) == 1
        await engine.dispose()
    asyncio.run(run())


def test_stale_review_and_edit_after_finalize_blocked(staging_sessions):
    from app.repositories.draft_order_repository import DraftOrderRepository
    from app.services.order_lifecycle import InvalidOrderTransitionError
    _, engine = staging_sessions
    async def run():
        repository = DraftOrderRepository()
        draft = await ready_draft()
        await repository.set_customer_type(draft.id, "retail")
        repository.expected_revision = 0
        with pytest.raises(InvalidOrderTransitionError):
            await repository.set_customer_type(draft.id, "wholesale")
        repository.expected_revision = None
        with pytest.raises(InvalidOrderTransitionError):
            await repository.save_review(draft.id, "ready", 39996, [],
                {draft.items[0].id: (9999, 39996)}, expected_revision=0)
        with pytest.raises(InvalidOrderTransitionError):
            await repository.finalize(draft.id)
        other = await ready_draft()
        await repository.finalize(other.id)
        with pytest.raises(InvalidOrderTransitionError):
            await repository.resolve_item(other.id, other.items[0].id, {"id": "different"})
        await engine.dispose()
    asyncio.run(run())


def test_concurrent_duplicate_email_is_idempotent(staging_sessions):
    from app.services.draft_order_service import DraftOrderService
    from app.services.product_matching_service import ProductMatchingService
    from app.services.price_service import PriceService
    from app.models.sales import CustomerType
    from tests.test_email_pipeline import email_message
    _, engine = staging_sessions
    async def run():
        products = SimpleNamespace(get_products=lambda: [{"id": "p1", "name": "Product",
            "salePrices": [{"value": 9999, "priceType": {"name": "W"}}]}])
        def service():
            return DraftOrderService(matching_service=ProductMatchingService(products),
                price_service=PriceService({CustomerType.WHOLESALE: "W"}),
                counterparty_service=SimpleNamespace(candidates_async=AsyncMock(return_value=[])), notifier=AsyncMock())
        message = email_message(uuid4().hex, "Product x2")
        from dataclasses import replace
        message = replace(message, sender_email=uuid4().hex + "@example.invalid")
        drafts = await asyncio.gather(*(service().ingest_email(message) for _ in range(5)))
        assert len({d.id for d in drafts}) == 1
        assert drafts[0].customer_type == "unknown"
        await engine.dispose()
    asyncio.run(run())


def test_checkout_concurrency_retail_review_and_conflict(staging_sessions):
    from app.services.checkout_service import CheckoutService, CheckoutConflict
    from app.schemas.checkout import CheckoutCreate
    from app.models.fulfillment import CheckoutRequest
    from sqlalchemy import select
    from tests.test_readiness_v2 import checkout_payload
    factory, engine = staging_sessions
    async def run():
        products = SimpleNamespace(get_catalog_async=AsyncMock(return_value=[{"id": "p1", "name": "Product",
            "price": None, "stocks": [{"id": "w1", "stock": 10, "reserve": 0}]}]))
        payload = CheckoutCreate(**{**checkout_payload(), "email": uuid4().hex + "@example.invalid"})
        key = uuid4().hex
        service = CheckoutService(products)
        responses = await asyncio.gather(*(service.submit(payload, key) for _ in range(5)))
        assert all(r == responses[0] and r["total_minor"] is None for r in responses)
        changed = payload.model_copy(update={"comment": "changed"})
        with pytest.raises(CheckoutConflict):
            await service.submit(changed, key)
        async with factory() as session:
            assert len((await session.execute(select(CheckoutRequest).where(CheckoutRequest.key == key))).scalars().all()) == 1
        await engine.dispose()
    asyncio.run(run())


def test_warehouse_split_persisted_once(staging_sessions):
    from app.repositories.draft_order_repository import DraftOrderRepository
    from app.services.fulfillment_service import FulfillmentService
    _, engine = staging_sessions
    async def run():
        draft = await ready_draft()
        order = await DraftOrderRepository().finalize(draft.id)
        service = FulfillmentService(SimpleNamespace(get_catalog_async=AsyncMock(return_value=[{
            "id": "p1", "stocks": [{"id": "w1", "stock": 3, "reserve": 1}, {"id": "w2", "stock": 4, "reserve": 1}]}])))
        plans = await asyncio.gather(service.plan(order.id), service.plan(order.id))
        assert {s.id for s in plans[0]} == {s.id for s in plans[1]}
        assert len(plans[0]) == 2
        assert sum(a.qty for s in plans[0] for a in s.allocations) == 4
        await engine.dispose()
    asyncio.run(run())


def test_phase3_review_round_trips_and_duration(staging_sessions):
    from sqlalchemy import event
    from app.services.draft_order_service import DraftOrderService
    from app.services.product_matching_service import ProductMatchingService
    from app.services.price_service import PriceService
    from app.models.sales import CustomerType
    _, engine = staging_sessions
    async def run():
        draft = await ready_draft()
        service = DraftOrderService(matching_service=ProductMatchingService(SimpleNamespace(get_products=lambda: [
            {"id": "p1", "name": "Product", "salePrices": [{"value": 9999, "priceType": {"name": "W"}}]}])),
            price_service=PriceService({CustomerType.WHOLESALE: "W"}), notifier=AsyncMock())
        queries = []
        def record(*args):
            queries.append(args[2].split()[0])
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        start = perf_counter()
        result = await service.set_customer_type(draft.id, CustomerType.WHOLESALE)
        duration = (perf_counter() - start) * 1000
        event.remove(engine.sync_engine, "before_cursor_execute", record)
        assert result.status == "ready" and result.total == 39996
        assert queries.count("SELECT") <= 8
        Path(".staging-artifacts/performance.json").write_text(json.dumps({
            "operation": "set_customer_type", "duration_ms": round(duration, 2),
            "sql_statements": len(queries), "selects": queries.count("SELECT"),
            "network": "Railway staging from local workstation", "moysklad": "fake catalog"}), encoding="utf-8")
        await engine.dispose()
    asyncio.run(run())


def test_manager_lifecycle_atomic_replay_audit_and_permissions(staging_sessions):
    from app.models.manager import Manager
    from app.models.operations import OrderEvent
    from app.models.order import Order
    from app.services.order_operations import OrderOperations, OrderAction, OperationError, ManagerDenied
    from app.repositories.draft_order_repository import DraftOrderRepository
    from app.services.fulfillment_service import FulfillmentService
    from sqlalchemy import select, func
    factory, engine = staging_sessions
    async def run():
        tid = int(uuid4().hex[:12], 16)
        async with factory() as session, session.begin():
            manager = Manager(telegram_id=tid, name="RC test", is_active=True)
            session.add(manager)
        draft = await ready_draft()
        order = await DraftOrderRepository().finalize(draft.id)
        service = OrderOperations()
        def req(action, version, **kw):
            return OrderAction(action=action, expected_revision=version, idempotency_key=f"{action}:{version}", **kw)
        with pytest.raises(ManagerDenied):
            await service.act(order.id, tid+1, req("paid", 0))
        with pytest.raises(OperationError):
            await service.act(order.id, tid, req("assembling", 0))
        products = SimpleNamespace(get_catalog_async=AsyncMock(return_value=[{"id": "p1", "stocks": [{"id": "w1", "stock": 2}, {"id": "w2", "stock": 2}]}]))
        await FulfillmentService(products).plan(order.id)
        expected = [("delivery", {"delivery_method": "manual"}), ("assembling", {}), ("assembled", {}), ("shipped", {}), ("paid", {"note": "RC verification"})]
        for version, (action, kwargs) in enumerate(expected):
            request = req(action, version, **kwargs)
            results = await asyncio.gather(*(service.act(order.id, tid, request) for _ in range(4)))
            assert all(o.revision == version+1 for o in results)
            async with factory() as session:
                persisted = await session.get(Order, order.id)
                assert persisted.revision == version+1 and persisted.total == 39996
                assert await session.scalar(select(func.count()).select_from(OrderEvent).where(OrderEvent.order_id == order.id)) == version+1
        assert results[0].paid_at and results[0].shipped_at and results[0].assembled_at
        assert results[0].paid_by_manager_id == manager.id
        assert results[0].moysklad_order_id is None
        with pytest.raises(OperationError):
            await service.act(order.id, tid, req("review", 0))
        # One of two different concurrent actions based on the same revision wins.
        actions = [req("review", 5), req("delivery", 5, delivery_status="delivered")]
        results = await asyncio.gather(*(service.act(order.id, tid, r) for r in actions), return_exceptions=True)
        assert sum(isinstance(r, OperationError) for r in results) == 1
        await engine.dispose()
    asyncio.run(run())


def test_client_conversation_replay_ownership_existing_identity_and_restart(staging_sessions):
    from app.services.client_channel import ClientChannel
    from app.models.customer import Customer, CustomerIdentity
    from app.models.draft_order import DraftOrder
    from app.models.notification import DraftNotification
    from sqlalchemy import select, func
    factory, engine = staging_sessions
    async def run():
        tid = int(uuid4().hex[:12], 16)
        phone = "+7" + str(tid)[-10:].zfill(10)
        async with factory() as session, session.begin():
            customer = Customer(customer_type="wholesale", display_name="Existing RC customer")
            customer.identities.append(CustomerIdentity(identity_type="phone", normalized_value=phone, original_value=phone))
            session.add(customer)
        await ClientChannel().handle(tid, 1, "/start")
        await ClientChannel().handle(tid, 2, "/request")
        await ClientChannel().handle(tid, 3, "Свеча; 2\nАромат; 1")
        await ClientChannel().handle(tid, 4, "Контакт", verified_phone=phone)
        responses = await asyncio.gather(*(ClientChannel().handle(tid, 5, "/send") for _ in range(5)))
        assert len(set(responses)) == 1
        import re
        draft_id = int(re.search(r"№(\d+)", responses[0]).group(1))
        async with factory() as session:
            draft = await session.get(DraftOrder, draft_id)
            assert draft.customer_id == customer.id and draft.customer_type == "wholesale"
            assert draft.source == "telegram" and draft.total is None and draft.status == "needs_review"
            assert await session.scalar(select(func.count()).select_from(DraftOrder).where(DraftOrder.customer_id == customer.id)) == 1
            assert await session.scalar(select(func.count()).select_from(DraftNotification).where(DraftNotification.draft_id == draft_id)) == 1
            identity = (await session.execute(select(CustomerIdentity).where(CustomerIdentity.identity_type == "telegram", CustomerIdentity.normalized_value == str(tid)))).scalar_one()
            assert identity.customer_id == customer.id
        assert "менеджер проверяет" in await ClientChannel().handle(tid, 6, f"/status {draft_id}")
        assert "не найдена" in await ClientChannel().handle(tid+1, 1, f"/status {draft_id}")
        assert "передана просьба" in await ClientChannel().handle(tid, 7, "/manager")
        await engine.dispose()
    asyncio.run(run())


def test_direct_order_concurrent_idempotency(staging_sessions):
    from app.repositories.order_repository import create_order
    from app.services.checkout_service import CheckoutConflict
    _, engine = staging_sessions
    async def run():
        data = {"_request_key": uuid4().hex, "_request_hash": "a"*64,
            "customer_name": "RC", "phone": "", "customer_type": "retail", "source": "manual", "total": 100,
            "items": [{"id": "p", "name": "Product", "price": 100, "qty": 1, "sum": 100}]}
        results = await asyncio.gather(*(create_order(data) for _ in range(5)))
        assert len({o.id for o in results}) == 1
        with pytest.raises(CheckoutConflict):
            await create_order({**data, "_request_hash": "b"*64})
        await engine.dispose()
    asyncio.run(run())


def test_poller_session_lock_has_one_owner(staging_sessions):
    from sqlalchemy import text
    _, engine = staging_sessions
    async def run():
        key = int(uuid4().hex[:12], 16)
        async with engine.connect() as first, engine.connect() as second:
            assert await first.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            assert not await second.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            await first.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            assert await second.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            await second.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        await engine.dispose()
    asyncio.run(run())


def test_unverified_client_contact_routes_existing_profile_without_identity_takeover(staging_sessions):
    from app.models.customer import Customer, CustomerIdentity
    from app.models.draft_order import DraftOrder
    from app.services.client_channel import ClientChannel
    from sqlalchemy import select, func
    factory, engine = staging_sessions
    async def run():
        tid = int(uuid4().hex[:12], 16)
        email = uuid4().hex + "@example.invalid"
        async with factory() as session, session.begin():
            customer = Customer(customer_type="wholesale", display_name="Existing")
            customer.identities.append(CustomerIdentity(identity_type="email", normalized_value=email, original_value=email))
            session.add(customer)
        for mid, message in enumerate(("/request", "Product; 1", email, "/send"), 1):
            response = await ClientChannel().handle(tid, mid, message)
        assert "Заявка №" in response
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(DraftOrder).where(DraftOrder.customer_id == customer.id)) == 1
            assert await session.scalar(select(func.count()).select_from(CustomerIdentity).where(CustomerIdentity.identity_type == "telegram", CustomerIdentity.normalized_value == str(tid))) == 0
        await engine.dispose()
    asyncio.run(run())
