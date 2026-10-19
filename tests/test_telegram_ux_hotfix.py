import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.database.base import Base
from app.models.telegram_card import TelegramCardAttempt
from app.models.order import Order
from app.services import telegram_cards as cards
from app.services.counterparty_matching_service import CounterpartyMatchingService as Matching
from tests.test_tilda_sku_review import database, catalog, get_draft, payload
from tests.test_tilda_orders import tilda_config
from tests.test_order_sources import source_db, seed_sources


@pytest.mark.parametrize("query", ["Davian", "Давиан", "ДАВИАН", "  давиан  "])
def test_75_transliteration(query):
    provider = SimpleNamespace(search_counterparties=lambda q: [
        {"id": "a", "name": "Давиан"}, {"id": "b", "name": "Davian", "archived": True}])
    result = Matching(provider).candidates(query)
    assert [r["id"] for r in result] == ["a"]
    assert "транслитерация" in result[0]["reason"]
    assert Matching.transliterate("Давиан") == "davian"


def test_exact_contacts_rank_first_and_limit():
    items = {str(n): {"id": str(n), "name": "Davian"} for n in range(8)}
    items["7"]["email"] = "person@example.com"
    result = Matching.ranked(items, ["Davian", "person@example.com"])
    assert len(result) == 5 and result[0]["id"] == "7"
    assert "email" in result[0]["reason"]


@pytest.fixture
def card_db(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "cards.db"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(setup())
    monkeypatch.setattr(cards, "async_session", factory)
    yield factory
    asyncio.run(engine.dispose())


def test_cards_dedup_restart_scope_and_failed_send(card_db):
    async def run():
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=[SimpleNamespace(message_id=n) for n in (10, 20, 30, 40)]),
            delete_message=AsyncMock())
        await cards.publish(bot, 1, "draft:77", "first")
        await cards.publish(bot, 1, "draft:75", "other")
        await cards.publish(bot, 2, "draft:77", "other chat")
        await cards.publish(bot, 1, "draft:77", "first")
        assert bot.send_message.await_count == 3
        # No process-local state: a new bot/session still finds the receipt.
        await cards.publish(bot, 1, "draft:77", "changed")
        bot.delete_message.assert_awaited_once_with(chat_id=1, message_id=10)
        assert await cards.obsolete(1, 10)
        assert not await cards.obsolete(1, 20)
        assert not await cards.obsolete(2, 30)
        assert not await cards.obsolete(1, 999)  # human message, never registered
        bot.send_message.side_effect = TimeoutError()
        with pytest.raises(TimeoutError):
            await cards.publish(bot, 1, "draft:77", "uncertain")
        with pytest.raises(cards.CardDeliveryUncertain):
            await cards.publish(bot, 1, "draft:77", "uncertain")
        assert bot.delete_message.await_count == 1
        async with card_db() as session:
            assert await session.scalar(select(func.count()).select_from(TelegramCardAttempt)
                .where(TelegramCardAttempt.status == "uncertain")) == 1
    asyncio.run(run())


def test_concurrent_card_sends_claim_before_network(card_db):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(message_id=10)
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=send), delete_message=AsyncMock())
        first = asyncio.create_task(cards.publish(bot, 1, "draft:77", "same"))
        await entered.wait()
        with pytest.raises(cards.CardDeliveryUncertain):
            await cards.publish(bot, 1, "draft:77", "same")
        release.set()
        await first
        bot.send_message.assert_awaited_once()
    asyncio.run(run())


def test_77_unknown_sku_can_select_own_x_without_creating_order(database, catalog, monkeypatch):
    from app.services import tilda_source_review as sources, order_desk
    monkeypatch.setattr(sources, "async_session", database)
    catalog.return_value = []
    async def run():
        did = (await order_desk.TildaIntake().submit(payload()))["draft_id"]
        await order_desk.DeskService().claim(did, 111, -100123456)
        before = await get_draft(database, did)
        await sources.record(did, 111, -100123456, before.revision)
        current = await get_draft(database, did)
        await sources.record(did, 111, -100123456, current.revision, item_id=current.items[0].id, source="own_x")
        after = await get_draft(database, did)
        assert after.contact_details["tilda_source_intents"][str(after.items[0].id)] == {
            "source": "own_x", "status": "identity_required"}
        assert (after.items[0].raw_product_text, after.items[0].qty, after.items[0].price) == (
            before.items[0].raw_product_text, before.items[0].qty, before.items[0].price)
        with pytest.raises(order_desk.DeskError):
            await order_desk.DeskService().confirm(did, 111, -100123456, after.revision)
        async with database() as session:
            assert await session.scalar(select(func.count()).select_from(Order)) == 0
    asyncio.run(run())


def test_owned_x_never_creates_supplier_purchase_or_settlement(source_db, monkeypatch):
    from app.schemas.order_sources import SourceAction
    from app.models.buying import BuyingPurchase
    from app.models.supply import XSettlement
    from app.models.fulfillment import WarehouseAllocation
    from app.integrations.moysklad.payloads import inventory_enter
    async def run():
        order, service, _ = await seed_sources(source_db)
        request = SourceAction(action="assign", expected_revision=0, idempotency_key="own-x",
            parts=[dict(item_id=order.items[0].id, qty=8, source_type="own_x", warehouse_id="wh1", base_cost_minor=50000)])
        view = await service.act(order.id, 111, -100123456, request)
        assert await service.act(order.id, 111, -100123456, request) == view
        assert view["items"][0]["ready"] == 0
        async with source_db() as session:
            assert await session.scalar(select(func.count()).select_from(BuyingPurchase)) == 0
            assert await session.scalar(select(func.count()).select_from(XSettlement)) == 0
            allocation = await session.scalar(select(WarehouseAllocation))
            assert allocation.source_type == "own_x" and allocation.own_cost_minor == 50000
            assert allocation.supplier_id is None
        document = inventory_enter("org", "wh1", [{"id": "ms-dsd", "qty": 8, "price": 50000}], "own-x:receipt")
        assert "agent" not in document and "purchaseOrder" not in document
        assert document["positions"][0]["price"] == 50000
        from app.config.settings import settings
        from app.integrations.moysklad.documents import FakeDocumentAdapter
        from app.services.external_operation_service import ExternalOperationService
        from app.services.warehouse_documents import WarehouseDocumentService
        from tests.test_ohmysmell_v1 import Store
        monkeypatch.setattr(settings, "moysklad_organization_id", "org")
        received = await service.act(order.id, 111, -100123456, SourceAction(action="receive",
            expected_revision=view["revision"], idempotency_key="own-x:received", allocation_id=allocation.id))
        adapter, store = FakeDocumentAdapter(), Store()
        documents = WarehouseDocumentService(adapter=adapter, operations=ExternalOperationService(store), sessions=source_db)
        posted = await documents.act(order.id, 111, -100123456, received["revision"], "receipt", allocation.id)
        assert posted == await documents.act(order.id, 111, -100123456, received["revision"], "receipt", allocation.id)
        assert adapter.calls == 1
        intent = next(iter(store.operations.values()))["payload"]
        assert intent["entity"] == "enter" and "agent" not in intent["document"]
        assert (await service.read(order.id, 111, -100123456))["items"][0]["ready"] == 8
    asyncio.run(run())


def test_manual_binding_survives_recheck_without_changing_source(database, catalog, monkeypatch):
    from app.services import tilda_source_review as sources, order_desk
    monkeypatch.setattr(sources, "async_session", database)
    catalog.return_value = [{"id": "unlisted", "name": "Site name", "article": "OTHER"}]
    async def run():
        did = (await order_desk.TildaIntake().submit(payload()))["draft_id"]
        await order_desk.DeskService().claim(did, 111, -100123456)
        before = await get_draft(database, did)
        draft = await sources.search(did, before.items[0].id, "Site name", 111, -100123456, before.revision)
        await sources.bind(did, draft.items[0].id, "unlisted", 111, -100123456, draft.revision)
        await order_desk.DeskService().reprice(did, 111, -100123456)
        await order_desk.DeskService().reprice(did, 111, -100123456)
        after = await get_draft(database, did)
        assert after.items[0].product_id == "unlisted"
        assert after.items[0].price == before.items[0].price
        assert after.items[0].qty == before.items[0].qty
        assert after.contact_details["external_products"] == before.contact_details["external_products"]
        order = await order_desk.DeskService().confirm(did, 111, -100123456, after.revision)
        assert order.items[0].product_id == "unlisted"
    asyncio.run(run())


def test_hint_cache_expiry_and_invalidation(monkeypatch):
    from app.services import tilda_availability as hints
    hints.invalidate()
    clock = [0]
    monkeypatch.setattr(hints, "monotonic", lambda: clock[0])
    fetch = AsyncMock(return_value={"p": 3})
    monkeypatch.setattr(hints, "_availability", fetch)
    async def run():
        items = [SimpleNamespace(product_id="p")]
        await hints.availability(items)
        await hints.availability(items)
        assert fetch.await_count == 1
        clock[0] = 2
        await hints.availability(items)
        assert fetch.await_count == 2
        hints.invalidate()
        await hints.availability(items)
        assert fetch.await_count == 3
    asyncio.run(run())
    hints.invalidate()


def test_directory_creation_guard_before_database(monkeypatch):
    from app.services.manager_directory import create_counterparty, create_product
    from app.config.settings import settings
    from app.integrations.write_guard import ExternalWritesDisabled
    monkeypatch.setattr(settings, "external_writes_enabled", False)
    with pytest.raises(ExternalWritesDisabled):
        asyncio.run(create_counterparty(75, 111, 0))
    with pytest.raises(ExternalWritesDisabled):
        asyncio.run(create_product(77, 1, 111, -100123456, 0))


def test_old_card_delete_failure_removes_buttons(card_db):
    from aiogram.exceptions import TelegramBadRequest
    async def run():
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=[SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]),
            delete_message=AsyncMock(side_effect=TelegramBadRequest(method=SimpleNamespace(), message="message can't be deleted")),
            edit_message_reply_markup=AsyncMock(), edit_message_text=AsyncMock())
        await cards.publish(bot, 1, "order:77", "first")
        await cards.publish(bot, 1, "order:77", "new")
        bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=1, message_id=1, reply_markup=None)
        assert "устарела" in bot.edit_message_text.call_args.kwargs["text"]
        assert await cards.obsolete(1, 1)
    asyncio.run(run())


def test_reply_is_bound_to_manager_prompt_and_draft(monkeypatch):
    import time
    from app.bot import manager_inputs as inputs
    from app.services import draft_order_service as draft_service
    inputs.pending.clear()
    inputs.processing.clear()
    inputs.pending[(1, 111)] = (time.monotonic() + 600, 10, "counterparty", {"draft_id": 75, "revision": 2})
    inputs.pending[(1, 222)] = (time.monotonic() + 600, 20, "counterparty", {"draft_id": 77, "revision": 3})
    saves = []
    async def save(did, candidates):
        saves.append(did)
        return SimpleNamespace(id=did)
    service = SimpleNamespace(repository=SimpleNamespace(set_counterparty_candidates=AsyncMock(side_effect=save)),
        counterparty_service=SimpleNamespace(candidates_async=AsyncMock(return_value=[])))
    monkeypatch.setattr(draft_service, "DraftOrderService", lambda: service)
    monkeypatch.setattr("app.services.draft_telegram_service.build_draft_card", lambda _: "card")
    monkeypatch.setattr("app.services.draft_telegram_service.build_draft_keyboard", lambda _: {"inline_keyboard": []})
    monkeypatch.setattr(cards, "publish", AsyncMock())
    bot = SimpleNamespace(delete_message=AsyncMock())
    def message(actor, prompt):
        return SimpleNamespace(chat=SimpleNamespace(id=1), from_user=SimpleNamespace(id=actor),
            reply_to_message=SimpleNamespace(message_id=prompt), text="Davian", bot=bot, answer=AsyncMock())
    async def run():
        assert not await inputs.route(message(222, 10), AsyncMock(return_value=True))
        await asyncio.gather(inputs.route(message(111, 10), AsyncMock(return_value=True)),
            inputs.route(message(222, 20), AsyncMock(return_value=True)))
        assert sorted(saves) == [75, 77]
        assert not inputs.pending
        assert sorted(call.kwargs["message_id"] for call in bot.delete_message.await_args_list) == [10, 20]
    asyncio.run(run())


def test_counterparty_changed_since_search_cannot_be_linked(monkeypatch):
    from app.services.draft_order_service import DraftOrderService, DraftOrderError
    candidate = {"id": "c", "name": "Davian", "inn": "123", "email": None, "phone": None}
    service = DraftOrderService()
    service._get_required = AsyncMock(return_value=SimpleNamespace(counterparty_candidates=[candidate]))
    service._ensure_reviewable = lambda _: None
    service.counterparty_service.async_gateway.get_counterparty = AsyncMock(return_value={**candidate, "inn": "456"})
    service.repository.set_counterparty = AsyncMock()
    with pytest.raises(DraftOrderError, match="Реквизиты"):
        asyncio.run(service.link_counterparty(75, "c", "Davian"))
    service.repository.set_counterparty.assert_not_awaited()


def test_reconcile_requires_exact_message_from_this_bot(card_db):
    async def run():
        bot = SimpleNamespace(id=123, send_message=AsyncMock(side_effect=TimeoutError()), delete_message=AsyncMock())
        with pytest.raises(TimeoutError):
            await cards.publish(bot, 1, "order:77", "exact card")
        def message(sender, body):
            return SimpleNamespace(from_user=SimpleNamespace(id=sender, is_bot=sender == 123),
                chat=SimpleNamespace(id=1), text=body, reply_markup=None, message_id=100)
        with pytest.raises(ValueError):
            await cards.reconcile(bot, "order:77", message(456, "exact card"))
        with pytest.raises(ValueError):
            await cards.reconcile(bot, "order:77", message(123, "different card"))
        await cards.reconcile(bot, "order:77", message(123, "exact card"))
        sent = await cards.publish(bot, 1, "order:77", "exact card")
        assert sent.message_id == 100 and bot.send_message.await_count == 1
    asyncio.run(run())
