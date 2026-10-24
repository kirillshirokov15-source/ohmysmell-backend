"""Opt-in Telegram card receipts must be restart-safe and never delete other chats."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.base import Base
from app.models.telegram_card import TelegramCard, TelegramCardAttempt
from app.services import telegram_cards as cards


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


def test_new_card_delivery_is_disabled_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("OMS_TELEGRAM_DURABLE_CARDS_ENABLED", raising=False)
    assert not cards.enabled()
    monkeypatch.setenv("OMS_TELEGRAM_DURABLE_CARDS_ENABLED", "true")
    assert cards.enabled()
    monkeypatch.setenv("OMS_TELEGRAM_DURABLE_CARDS_ENABLED", "false")
    assert not cards.enabled()


def test_durable_cards_replay_and_scope(card_db):
    async def run():
        sent = [SimpleNamespace(message_id=n) for n in (10, 20, 30, 40)]
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=sent),
                              delete_message=AsyncMock())
        await cards.publish(bot, -1001, "draft:75", "one")
        await cards.publish(bot, -1001, "draft:77", "other")
        await cards.publish(bot, -1002, "draft:75", "different chat")
        await cards.publish(bot, -1001, "draft:75", "one")
        assert bot.send_message.await_count == 3
        await cards.publish(bot, -1001, "draft:75", "changed")
        assert bot.send_message.await_count == 4
        bot.delete_message.assert_awaited_once_with(chat_id=-1001, message_id=10)
        assert await cards.obsolete(-1001, 10)
        assert not await cards.obsolete(-1001, 20)
        assert not await cards.obsolete(-1002, 30)
        assert not await cards.obsolete(-1001, 999)
    asyncio.run(run())


def test_unknown_send_result_never_retries_blindly(card_db):
    async def run():
        bot = SimpleNamespace(id=13, send_message=AsyncMock(side_effect=TimeoutError()),
                              delete_message=AsyncMock())
        with pytest.raises(TimeoutError):
            await cards.publish(bot, -1003, "order:8", "exact")
        with pytest.raises(cards.CardDeliveryUncertain):
            await cards.publish(bot, -1003, "order:8", "exact")
        assert bot.send_message.await_count == 1
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=13, is_bot=True),
            chat=SimpleNamespace(id=-1003),
            text="exact", reply_markup=None, message_id=56)
        with pytest.raises(ValueError):
            await cards.reconcile(bot, "order:8", SimpleNamespace(
                from_user=SimpleNamespace(id=500, is_bot=False),
                chat=message.chat, text=message.text,
                reply_markup=None, message_id=56))
        await cards.reconcile(bot, "order:8", message)
        await cards.publish(bot, -1003, "order:8", "exact")
        assert bot.send_message.await_count == 1
    asyncio.run(run())
