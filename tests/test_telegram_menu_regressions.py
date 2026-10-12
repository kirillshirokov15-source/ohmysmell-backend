"""Real dispatch + local SQLite + fake Telegram. No staging or provider writes."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage, EditMessageReplyMarkup, GetUpdates
from aiogram.types import Chat, Message, User, Update, InlineKeyboardMarkup, ReplyKeyboardRemove
from sqlalchemy import select

from tests.telegram_transport import TelegramSession
from tests.test_tilda_sku_review import database, catalog, tilda_config
from app.config.settings import settings
from app.models.customer import Customer
from app.models.inbound_message import InboundMessage
from app.models.draft_order import DraftOrder
from app.models.order_desk import OrderDesk, DeskMessage
from app.bot.client_bot import create_dispatcher
from app.bot.manager_menu import show_menu
from app.workers import desk_outbox


def inbound(text, number, actor=777, group=False):
    return Message(message_id=number, date=datetime.now(timezone.utc), text=text,
        chat=Chat(id=-100123 if group else actor, type="supergroup" if group else "private"),
        from_user=User(id=actor, first_name="Synthetic", is_bot=False))


class RememberingTelegram(TelegramSession):
    """Keyboard state lives in Telegram, surviving new Bot/dispatcher instances."""
    def __init__(self, state):
        super().__init__()
        self.state = state

    async def make_request(self, bot, method, timeout=None):
        result = await super().make_request(bot, method, timeout)
        if isinstance(method, SendMessage):
            result = result.model_copy(update={"message_id": len(self.state["messages"]) + 1000})
            self.state["messages"][result.message_id] = method.model_copy()
            if isinstance(method.reply_markup, ReplyKeyboardRemove):
                assert method.reply_markup.selective
                assert not method.reply_parameters.allow_sending_without_reply
                actor = self.state["authors"][method.reply_parameters.message_id]
                self.state["keyboards"][actor] = None
            else:
                self.state["keyboards"][method.chat_id] = method.reply_markup
        elif isinstance(method, EditMessageReplyMarkup):
            self.state["messages"][method.message_id] = self.state["messages"][method.message_id].model_copy(
                update={"reply_markup": method.reply_markup})
        return result


@pytest.mark.parametrize("legacy", ["six_buttons", "Новые заказы / Все заказы"])
def test_group_removes_both_legacy_keyboards_selectively_in_one_message(legacy):
    async def run():
        state = dict(keyboards={777: legacy, 888: legacy}, messages={}, authors={1: 777, 2: 777})
        for number in (1, 2):
            # Restarted Bot, same Telegram-side keyboard history. Each intentional
            # /start produces ONE visible message, not removal + duplicate menu.
            transport = RememberingTelegram(state)
            bot = Bot("123456:FAKE", session=transport)
            await show_menu(inbound("/start", number, group=True).as_(bot))
            assert state["keyboards"] == {777: None, 888: legacy}
            assert len(state["messages"]) == number
            menu = state["messages"][999 + number].reply_markup
            assert isinstance(menu, InlineKeyboardMarkup)
            assert sum(map(len, menu.inline_keyboard)) == 3
            assert all(b.callback_data.startswith("menu:777:") for row in menu.inline_keyboard for b in row)
            assert not any(call.__api_method__ in {"setMyCommands", "deleteMyCommands", "deleteMessage"} for call in transport.calls)
            await bot.session.close()
    asyncio.run(run())


def test_private_replaces_old_keyboard_with_three_actions():
    async def run():
        state = dict(keyboards={777: "legacy_categories"}, messages={}, authors={})
        bot = Bot("123456:FAKE", session=RememberingTelegram(state))
        await show_menu(inbound("/start", 1).as_(bot))
        keyboard = state["keyboards"][777]
        assert [b.text for row in keyboard.keyboard for b in row] == ["📦 Заказы", "🔎 Найти заказ", "☰ Ещё"]
        assert keyboard.is_persistent and len(state["messages"]) == 1
        await bot.session.close()
    asyncio.run(run())


async def seed(factory):
    async with factory() as session, session.begin():
        customer = Customer(display_name="Synthetic", customer_type="retail")
        session.add(customer)
        await session.flush()
        for identifier, actor in ((36, 777), (37, 777), (39, 888)):
            message = InboundMessage(source="website", external_message_id=f"fake-{identifier}",
                sender="synthetic", body_text="", received_at=datetime.now(timezone.utc), customer_id=customer.id)
            session.add(message)
            await session.flush()
            session.add(DraftOrder(id=identifier, inbound_message_id=message.id, customer_id=customer.id,
                customer_type="retail", source="website", sender_email="", status="needs_review"))
            await session.flush()
            session.add(OrderDesk(draft_id=identifier, external_id=f"fake-{identifier}", payload_hash="synthetic",
                customer_telegram_id=actor, stage="new"))


def configure(monkeypatch, factory):
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(desk_outbox, "async_session", factory)
    for key, value in dict(CLIENT_ORDER_DESK_ENABLED="true", ORDER_DESK_SEND_ENABLED="true",
        ORDER_DESK_STAGING_CLIENT_RECIPIENT_IDS="777", ORDER_DESK_STAGING_DRAFT_IDS="36",
        ORDER_DESK_STAGING_MESSAGE_IDS="", ORDER_DESK_STAGING_NOT_BEFORE=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()).items():
        monkeypatch.setenv(key, value)


def test_client_commands_scope_dispatch_replay_and_worker_restart(database, monkeypatch):
    async def run():
        configure(monkeypatch, database)
        await seed(database)
        for number, text in enumerate(("/start", "/status", "/status 36", "/start"), 1):
            transport = TelegramSession()
            bot = Bot("654321:FAKE", session=transport)
            update = Update(update_id=number, message=inbound(text, number))
            await create_dispatcher().feed_update(bot, update)
            # A dispatcher restart/replayed webhook cannot create a second ACK.
            await create_dispatcher().feed_update(bot, update)
            assert transport.calls == []  # Responses always go through the guarded queue.
            assert await desk_outbox.run_once(bot, "client")
            assert not await desk_outbox.run_once(bot, "client")
            assert len(transport.calls) == 1 and isinstance(transport.calls[0], SendMessage)
            reply = transport.calls[0]
            assert "/status" not in reply.text and "№37" not in reply.text and "№39" not in reply.text
            assert "Заявка №37" not in str(reply.reply_markup)
            if text == "/status 36":
                assert "№36" in reply.text
            await create_dispatcher().feed_update(bot, update)
            assert not await desk_outbox.run_once(bot, "client")
            await bot.session.close()
        async with database() as session:
            rows = (await session.scalars(select(DeskMessage).order_by(DeskMessage.id))).all()
            assert len(rows) == 4
            assert all(r.status == "sent" and r.draft_id == 36 and r.attempts == 1 for r in rows)
            assert [r.source_message_id for r in rows] == [1, 2, 3, 4]
    asyncio.run(run())


def test_durable_polling_commits_commands_before_offset_and_deduplicates_restart(database, monkeypatch):
    async def run():
        from app.bot.runtime import durable_client_poll
        configure(monkeypatch, database)
        await seed(database)
        update = Update(update_id=501, message=inbound("/start", 41))

        class PollingTelegram(TelegramSession):
            async def make_request(self, bot, method, timeout=None):
                if isinstance(method, GetUpdates):
                    self.calls.append(method)
                    if method.offset is None:
                        return [update]
                    assert method.offset == 502
                    raise asyncio.CancelledError()
                return await super().make_request(bot, method, timeout)

        for _ in range(2):
            bot = Bot("654321:FAKE", session=PollingTelegram())
            with pytest.raises(asyncio.CancelledError):
                await durable_client_poll(create_dispatcher(), bot)
            await bot.session.close()
        async with database() as session:
            rows = (await session.scalars(select(DeskMessage))).all()
            assert len(rows) == 1 and rows[0].status == "pending"
        bot = Bot("654321:FAKE", session=TelegramSession())
        assert await desk_outbox.run_once(bot, "client")
        assert not await desk_outbox.run_once(bot, "client")
        assert len([c for c in bot.session.calls if isinstance(c, SendMessage)]) == 1
        await bot.session.close()
    asyncio.run(run())


@pytest.mark.parametrize("block", ["disabled", "recipient", "draft", "time", "ownership", "message_only"])
def test_client_never_bypasses_staging_guards(database, monkeypatch, block):
    async def run():
        configure(monkeypatch, database)
        await seed(database)
        if block == "disabled": monkeypatch.setenv("ORDER_DESK_SEND_ENABLED", "false")
        if block == "recipient": monkeypatch.setenv("ORDER_DESK_STAGING_CLIENT_RECIPIENT_IDS", "999")
        if block == "draft": monkeypatch.setenv("ORDER_DESK_STAGING_DRAFT_IDS", "39")
        if block == "time": monkeypatch.setenv("ORDER_DESK_STAGING_NOT_BEFORE", "2099-01-01T00:00:00Z")
        if block == "message_only":
            monkeypatch.setenv("ORDER_DESK_STAGING_DRAFT_IDS", "")
            monkeypatch.setenv("ORDER_DESK_STAGING_MESSAGE_IDS", "99999")
        transport = TelegramSession()
        bot = Bot("654321:FAKE", session=transport)
        # Unknown ownership produces a draftless error; it must not borrow a scope.
        text = "/status 39" if block == "ownership" else "/status 36"
        await create_dispatcher().feed_update(bot, Update(update_id=1, message=inbound(text, 1)))
        assert not await desk_outbox.run_once(bot, "client")
        assert transport.calls == []
        async with database() as session:
            row = await session.scalar(select(DeskMessage))
            assert row.status == "pending" and row.attempts == 0
        await bot.session.close()
    asyncio.run(run())
