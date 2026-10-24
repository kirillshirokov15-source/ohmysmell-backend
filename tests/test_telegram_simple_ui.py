import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from sqlalchemy import select, func

from tests.test_tilda_sku_review import database, catalog, tilda_config, payload
from tests.test_order_sources import source_db, seed_sources
from tests.test_manager_menu import dispatch
from app.models.order_desk import DeskMessage, OrderDesk
from app.models.operations import ClientConversation, ClientUpdate
from app.services.order_desk import DeskService, TildaIntake, status_notification
from app.workers import desk_outbox


def test_three_manager_buttons_and_single_group_menu(dispatch, monkeypatch):
    from app.bot.manager_menu import ROWS
    assert [text for row in ROWS for text in row] == ["📦 Заказы", "🔎 Найти заказ", "☰ Ещё"]
    assert dispatch("/menu")[0].reply_markup.is_persistent
    dispatch("📦 Заказы")
    dispatch.orders.assert_awaited_once()
    assert len(dispatch("🔎 Найти заказ")) == 1
    assert len(dispatch("☰ Ещё")) == 1
    monkeypatch.setenv("MANAGER_TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setenv("MANAGER_TELEGRAM_USER_IDS", "11")
    menu = dispatch("/menu", group=True)
    from aiogram.methods import SendMessage
    assert len([call for call in menu if isinstance(call, SendMessage)]) == 2
    assert all(b.callback_data.startswith("menu:11:") for row in menu[-1].reply_markup.inline_keyboard for b in row)
    assert dispatch("Обычная переписка", group=True) == []
    assert dispatch("☰ Ещё", group=True, actor=12) == []


def test_compact_card_only_exposes_next_step_and_preserves_actions():
    from tests.test_release_candidate import order
    from app.services.manager_workspace import order_card, order_keyboard
    o = order(items=[SimpleNamespace(id=1, qty=2, name="Тестовый товар", price=50000)], ui_supply_ready=True)
    rows = order_keyboard(o)["inline_keyboard"]
    assert len(rows) == 2 and rows[0][0]["text"] == "Начать сборку"
    text = order_card(o)
    assert "Заказ №1" in text and "Товары:" in text and "Оплата:" in text
    assert "order:paid:1:0" in str(order_keyboard(o, expanded=True))
    o.needs_review = True
    assert "assembling" not in str(order_keyboard(o, expanded=True))
    o.fulfillment_status = "cancelled"
    assert [b["text"] for row in order_keyboard(o)["inline_keyboard"] for b in row] == ["⋯ Действия"]


def test_client_navigation_persists_selection_and_rechecks_ownership(database, catalog):
    async def run():
        identifiers = []
        for number in range(2):
            data = payload()
            data["external_id"] = f"navigation-{number}"
            did = (await TildaIntake().submit(data))["draft_id"]
            identifiers.append(did)
            async with database() as session, session.begin():
                (await session.get(OrderDesk, did)).customer_telegram_id = 777
        service = DeskService()
        await service.client(777, 1, "💬 Написать менеджеру")
        await service.client(777, 2, f"Заявка №{identifiers[1]}")
        # A fresh service instance recovers selection from the database.
        await DeskService().client(777, 3, "Когда привезёте?")
        await DeskService().client(777, 3, "Когда привезёте?")
        await DeskService().client(888, 1, f"Заявка №{identifiers[1]}")
        async with database() as session:
            messages = (await session.scalars(select(DeskMessage).where(DeskMessage.sender_type == "customer"))).all()
            assert len(messages) == 1 and messages[0].draft_id == identifiers[1]
            assert (await session.get(ClientConversation, 777)).state == "idle"
            assert "не найден" in (await session.get(ClientUpdate, "desk:client:888:1")).response
            assert await session.scalar(select(func.count()).select_from(ClientUpdate)) == 4
        await DeskService().client(777, 4, "🔎 Статус заказа")
        from app.bot.client_menu import delivery_keyboard
        async with database() as session:
            keyboard = await delivery_keyboard(session, 777)
            assert [row[0].text for row in keyboard.keyboard[:2]] == [f"Заявка №{d}" for d in reversed(identifiers)]
        await DeskService().client(777, 5, "Заказ №" + "9" * 500)
        async with database() as session:
            assert "Выберите заказ" in (await session.get(ClientUpdate, "desk:client:777:5")).response
    asyncio.run(run())


async def linked_card(factory, draft_id, key, body, *, status="pending", receipt=None):
    async with factory() as session, session.begin():
        row = DeskMessage(draft_id=draft_id, idempotency_key=key, sender_type="system",
            direction="to_customer", destination=777, kind="client_card", body=body,
            status=status, telegram_message_id=receipt)
        session.add(row)
        await session.flush()
        return row


@pytest.mark.parametrize("failure", [None, "message is not modified", "message to edit not found", "message can't be edited", "chat not found", "timeout"])
def test_durable_edit_and_safe_fallback(database, catalog, monkeypatch, failure):
    async def run():
        monkeypatch.setattr(desk_outbox, "async_session", database)
        did = (await TildaIntake().submit(payload()))["draft_id"]
        await linked_card(database, did, "previous", "Получен", status="sent", receipt=91)
        current = await linked_card(database, did, "current", "Собираем заказ")
        bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock(return_value=SimpleNamespace(message_id=92)))
        if failure:
            bot.edit_message_text.side_effect = TimeoutError() if failure == "timeout" else TelegramBadRequest(
                method=EditMessageText(text="x"), message=failure)
        if failure in {"chat not found", "timeout"}:
            with pytest.raises((TelegramBadRequest, TimeoutError)):
                await desk_outbox.deliver_client_card(bot, current)
            bot.send_message.assert_not_awaited()
        else:
            result = await desk_outbox.deliver_client_card(bot, current)
            fallback = failure in {"message to edit not found", "message can't be edited"}
            assert result.message_id == (92 if fallback else 91)
            assert bot.send_message.await_count == int(fallback)
        assert bot.edit_message_text.call_args.kwargs["message_id"] == 91
    asyncio.run(run())


def test_worker_restart_two_orders_and_unknown_send_do_not_duplicate(database, catalog, monkeypatch):
    async def run():
        from app.config.settings import settings
        monkeypatch.setattr(settings, "environment", "development")
        monkeypatch.setattr(desk_outbox, "async_session", database)
        bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock(side_effect=[
            SimpleNamespace(message_id=101), SimpleNamespace(message_id=102)]))
        ids = []
        for n in range(2):
            data = payload(); data["external_id"] = f"cards-{n}"
            did = (await TildaIntake().submit(data))["draft_id"]; ids.append(did)
            await linked_card(database, did, f"first-{n}", "Получен")
        assert await desk_outbox.run_once(bot, "client")
        assert await desk_outbox.run_once(bot, "client")
        for n, did in enumerate(ids):
            await linked_card(database, did, f"next-{n}", "Собираем")
        # No process-local receipt state is carried into this new bot instance.
        restarted = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock())
        assert await desk_outbox.run_once(restarted, "client")
        assert await desk_outbox.run_once(restarted, "client")
        assert {c.kwargs["message_id"] for c in restarted.edit_message_text.call_args_list} == {101, 102}
        restarted.send_message.assert_not_awaited()
        assert not await desk_outbox.run_once(restarted, "client")
        restarted.edit_message_text.side_effect = TimeoutError()
        row = await linked_card(database, ids[0], "uncertain", "Готов")
        assert await desk_outbox.run_once(restarted, "client")
        async with database() as session:
            assert (await session.get(DeskMessage, row.id)).status == "uncertain"
        assert not await desk_outbox.run_once(restarted, "client")
        restarted.send_message.assert_not_awaited()
    asyncio.run(run())


def test_important_events_retained_assembly_only_updates_card(source_db, catalog):
    async def run():
        order, _, _ = await seed_sources(source_db)
        async with source_db() as session, session.begin():
            desk = await session.scalar(select(OrderDesk).where(OrderDesk.order_id == order.id))
            desk.customer_telegram_id = 777
            await status_notification(session, order, 111)
        for revision, status in enumerate(("assembling", "assembled", "shipped"), 1):
            order.revision, order.fulfillment_status = revision, status
            if status == "shipped":
                order.delivery_status = "dispatched"
            async with source_db() as session, session.begin():
                persisted = await session.merge(order)
                await status_notification(session, persisted, 111)
        async with source_db() as session:
            rows = (await session.scalars(select(DeskMessage).where(DeskMessage.direction == "to_customer"))).all()
            assert len([r for r in rows if r.kind == "client_card"]) == 4
            notices = [r for r in rows if r.kind == "text"]
            assert len(notices) == 1 and "Передан в доставку" in notices[0].body
        await DeskService().client(777, 991, "💬 Написать менеджеру")
        await DeskService().client(777, 992, f"Заказ №{order.id}")
        async with source_db() as session:
            conversation = await session.get(ClientConversation, 777)
            assert conversation.state == "desk_message" and conversation.data["draft_id"] == desk.draft_id
        await DeskService().client(777, 993, f"/status {desk.draft_id}")
        async with source_db() as session:
            assert (await session.get(ClientConversation, 777)).state == "idle"
    asyncio.run(run())


def test_source_buttons_reuse_service_and_bind_actor(source_db, catalog, dispatch, monkeypatch):
    from app.bot import source_navigation, telegram_bot
    from app.services.product_service import ProductService
    monkeypatch.setenv("MANAGER_TELEGRAM_CHAT_ID", "-100123456")
    monkeypatch.setenv("MANAGER_TELEGRAM_USER_IDS", "111,222")
    order, service, _ = asyncio.run(seed_sources(source_db))
    monkeypatch.setattr(source_navigation, "OrderSourceService", lambda: service)
    monkeypatch.setattr(source_navigation, "async_session", source_db)
    monkeypatch.setattr("app.repositories.order_repository.get_order", AsyncMock(return_value=order))
    monkeypatch.setattr(telegram_bot, "get_order", AsyncMock(return_value=order))
    monkeypatch.setattr(ProductService, "get_catalog_async", service.products.get_catalog_async)
    def send(data, actor=111):
        return dispatch(None, group=True, chat_id=-100123456, actor=actor, callback=data)
    def choose(calls, prefix):
        return next(b.callback_data for call in calls if getattr(call, "reply_markup", None)
                    for row in call.reply_markup.inline_keyboard for b in row if b.text.startswith(prefix))
    calls = send(f"order:sources:{order.id}:0")
    item = choose(calls, "Источник:")
    assert len(send(item, actor=222)) == 1  # callback acknowledgement only
    calls = send(item)
    assert all(any(label == b.text for call in calls if getattr(call, "reply_markup", None)
        for row in call.reply_markup.inline_keyboard for b in row) for label in ("Свой склад", "Склад Х", "Внешний поставщик"))
    calls = send(choose(calls, "Свой склад"))
    calls = send(choose(calls, "wh1"))
    calls = send(choose(calls, "Подтвердить"))
    view = asyncio.run(service.read(order.id, 111, -100123456))
    assert view["items"][0]["ready"] == 8
    assert view["revision"] == 1


def test_manager_reply_requires_matching_prompt_and_cancel_clears_it(dispatch, monkeypatch):
    from app.bot.manager_inputs import pending
    from time import monotonic
    from app.services.order_desk import DeskService
    monkeypatch.setenv("MANAGER_TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setenv("MANAGER_TELEGRAM_USER_IDS", "11")
    reply = AsyncMock()
    monkeypatch.setattr(DeskService, "reply", reply)
    pending[(-100123, 11)] = (monotonic() + 60, 99, "reply", {"draft_id": 1})
    assert dispatch("Обычная переписка", group=True) == []
    assert dispatch("Ответ", group=True, reply=98) == []
    dispatch("Ответ", group=True, reply=99)
    reply.assert_awaited_once_with(1, 11, -100123, 42, "Ответ")
    pending[(-100123, 11)] = (monotonic() + 60, 99, "reply", {"draft_id": 1})
    dispatch("/cancel", group=True)
    assert (-100123, 11) not in pending


def test_release_preflight_requires_staging_and_preserves_safety():
    from app.scripts.staging_bots_release import validate_variables
    variables = dict(APP_ENV="staging", EXTERNAL_WRITES_ENABLED="false", CLIENT_ORDER_DESK_ENABLED="true")
    validate_variables("client", variables)
    for bad in ({"APP_ENV": "production"}, {"EXTERNAL_WRITES_ENABLED": "true"},
                {"ORDER_DESK_SEND_ENABLED": "true"}, {"CLIENT_ORDER_DESK_ENABLED": "false"}):
        with pytest.raises(RuntimeError):
            validate_variables("client", {**variables, **bad})
    validate_variables("client", {**variables, "ORDER_DESK_SEND_ENABLED": "true",
        "ORDER_DESK_STAGING_CLIENT_RECIPIENT_IDS": "777", "ORDER_DESK_STAGING_MESSAGE_IDS": "123"})


def test_draft_compact_view_keeps_original_versioned_actions():
    from app.services.draft_telegram_service import compact_draft_keyboard, build_draft_keyboard
    draft = SimpleNamespace(id=1, revision=0, source="email", status="needs_review",
        contact_details={}, counterparty_id=None, counterparty_candidates=[],
        items=[SimpleNamespace(id=1, qty=1, match_status="ambiguous", candidates=[], product_id=None)])
    rows = compact_draft_keyboard(draft)["inline_keyboard"]
    assert len(rows) == 3
    assert [row[0]["text"] for row in rows] == ["Проверить заявку", "Подробнее", "⋯ Действия"]
    assert compact_draft_keyboard(draft, expanded=True) == build_draft_keyboard(draft)


def test_expired_delivery_lease_never_resends(database, catalog, monkeypatch):
    async def run():
        from datetime import timedelta
        from app.services.order_desk import now
        from app.config.settings import settings
        monkeypatch.setattr(settings, "environment", "development")
        monkeypatch.setattr(desk_outbox, "async_session", database)
        did = (await TildaIntake().submit(payload()))["draft_id"]
        row = await linked_card(database, did, "expired", "Важное событие", status="sending")
        async with database() as session, session.begin():
            (await session.get(DeskMessage, row.id)).available_at = now() - timedelta(minutes=1)
        bot = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
        assert not await desk_outbox.run_once(bot, "client")
        async with database() as session:
            current = await session.get(DeskMessage, row.id)
            assert current.status == "uncertain" and current.body == "Важное событие"
        bot.send_message.assert_not_awaited()
        bot.edit_message_text.assert_not_awaited()
        # A separate important event must not be stuck behind an uncertain card edit.
        async with database() as session, session.begin():
            session.add(DeskMessage(draft_id=did, idempotency_key="important", sender_type="system",
                direction="to_customer", destination=777, kind="text", body="Нужна оплата", status="pending"))
        bot.send_message.return_value = SimpleNamespace(message_id=999)
        assert await desk_outbox.run_once(bot, "client")
        bot.send_message.assert_awaited_once()
        assert not await desk_outbox.run_once(bot, "client")
    asyncio.run(run())
