"""Real PostgreSQL locking with a fake Telegram transport; no external sends."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from tests.test_postgres_staging import staging_sessions
from tests.test_order_sources_postgres import sources_postgres, desk_db, tilda_config
from app.services import telegram_cards as cards

pytestmark = pytest.mark.staging


def test_card_claim_survives_concurrent_worker_and_restart(staging_sessions, monkeypatch):
    factory, engine = staging_sessions
    monkeypatch.setattr(cards, "async_session", factory)
    async def run():
        entity = "order:" + uuid4().hex[:20]
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(message_id=101)
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=send), delete_message=AsyncMock())
        task = asyncio.create_task(cards.publish(bot, -999, entity, "synthetic"))
        await entered.wait()
        try:
            with pytest.raises(cards.CardDeliveryUncertain):
                await cards.publish(bot, -999, entity, "synthetic")
        finally:
            release.set()
        await task
        await engine.dispose()
        # Reopen all connections; there is no in-memory delivery receipt.
        await cards.publish(bot, -999, entity, "synthetic")
        bot.send_message.assert_awaited_once()
        bot.send_message.side_effect = TimeoutError()
        with pytest.raises(TimeoutError):
            await cards.publish(bot, -999, entity, "changed")
        await engine.dispose()
        with pytest.raises(cards.CardDeliveryUncertain):
            await cards.publish(bot, -999, entity, "changed")
        await engine.dispose()
    asyncio.run(run())


def test_owned_x_receipt_and_reservation_use_durable_document_journal(sources_postgres, monkeypatch):
    from sqlalchemy import select, func
    from tests.test_order_sources import seed_sources, CHAT
    from app.config.settings import settings
    from app.schemas.order_sources import SourceAction
    from app.models.order import Order
    from app.models.supply import XSettlement
    from app.models.fulfillment import ExternalOperation
    from app.integrations.moysklad.documents import FakeDocumentAdapter
    from app.services.warehouse_documents import WarehouseDocumentService
    factory, engine, actor, pid = sources_postgres
    monkeypatch.setattr(settings, "moysklad_organization_id", "synthetic-org")
    async def run():
        order, source, _ = await seed_sources(factory, actor, pid=pid)
        async with factory() as session, session.begin():
            (await session.get(Order, order.id)).counterparty_id = "synthetic-customer"
        request = SourceAction(action="assign", expected_revision=0, idempotency_key=uuid4().hex,
            parts=[dict(item_id=order.items[0].id, qty=8, source_type="own_x", warehouse_id="wh1", base_cost_minor=50000)])
        view = await source.act(order.id, actor, CHAT, request)
        assert await source.act(order.id, actor, CHAT, request) == view
        allocation = view["allocations"][0]
        assert allocation["purchase_id"] is None and allocation["supplier_id"] is None
        assert not allocation["ready"]
        view = await source.act(order.id, actor, CHAT, SourceAction(action="receive", allocation_id=allocation["id"],
            expected_revision=view["revision"], idempotency_key=uuid4().hex))
        adapter = FakeDocumentAdapter()
        docs = WarehouseDocumentService(adapter=adapter, sessions=factory)
        result = await docs.act(order.id, actor, CHAT, view["revision"], "receipt", allocation["id"])
        assert result == await docs.act(order.id, actor, CHAT, view["revision"], "receipt", allocation["id"])
        async with factory() as session:
            receipt = await session.get(ExternalOperation, f"simulation:warehouse:allocation:{allocation['id']}:receipt")
            assert receipt.payload["entity"] == "enter" and "agent" not in receipt.payload["document"]
            assert await session.scalar(select(func.count()).select_from(XSettlement).where(XSettlement.order_id == order.id)) == 0
        view = await source.read(order.id, actor, CHAT)
        assert view["items"][0]["ready"] == 8
        await docs.act(order.id, actor, CHAT, view["revision"], "reserve", allocation["plan_id"])
        assert adapter.calls == 2
        await engine.dispose()
    asyncio.run(run())
