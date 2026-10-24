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


def test_manager_draft_refresh_uses_receipt_transport_only_when_enabled(monkeypatch):
    from app.bot import telegram_bot
    msg = SimpleNamespace(
        bot=SimpleNamespace(id=15),
        chat=SimpleNamespace(id=-1007),
        edit_text=AsyncMock())
    delivered = AsyncMock(return_value=SimpleNamespace(message_id=900))
    monkeypatch.setattr(cards, "publish", delivered)
    monkeypatch.setenv("OMS_TELEGRAM_DURABLE_CARDS_ENABLED", "true")
    assert asyncio.run(telegram_bot.safe_edit_message(msg, "card", draft_id=75)) is True
    delivered.assert_awaited_once()
    args = delivered.await_args.args
    assert args[1:4] == (-1007, "draft:75", "card")
    msg.edit_text.assert_not_awaited()
    monkeypatch.setenv("OMS_TELEGRAM_DURABLE_CARDS_ENABLED", "false")
    assert asyncio.run(telegram_bot.safe_edit_message(msg, "legacy", draft_id=75)) is True
    msg.edit_text.assert_awaited_once_with("legacy", reply_markup=None)
    delivered.assert_awaited_once()


def test_callback_ack_samples_quantiles_without_real_telegram(monkeypatch):
    from app.bot import telegram_bot
    from app.services.telegram_metrics import samples
    samples["callback_ack"].clear()
    callback = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=1))
    assert asyncio.run(telegram_bot.safe_callback_answer(callback))
    assert len(samples["callback_ack"]) == 1
    assert samples["callback_ack"][0] >= 0

def test_order_confirmation_prompt_is_receipted_and_refresh_replaces_it(card_db, monkeypatch):
    """A confirmation prompt must not leave a stale digest in the order receipt."""
    from app.bot import telegram_bot

    monkeypatch.setenv("OMS_TELEGRAM_DURABLE_CARDS_ENABLED", "true")

    async def run():
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=[
                SimpleNamespace(message_id=111),
                SimpleNamespace(message_id=112),
                SimpleNamespace(message_id=113),
            ]),
            delete_message=AsyncMock(),
        )
        message = SimpleNamespace(
            bot=bot, chat=SimpleNamespace(id=-1010), edit_text=AsyncMock()
        )
        await cards.publish(bot, -1010, "order:8", "Текущий заказ")
        assert await telegram_bot.safe_edit_message(
            message, "Отменить этот неоплаченный заказ?",
            draft_id=0, order_id=8,
        )
        assert await telegram_bot.safe_edit_message(
            message, "Текущий заказ", draft_id=0, order_id=8,
        )
        assert bot.send_message.await_count == 3
        assert bot.delete_message.await_count == 2
        bot.delete_message.assert_any_await(chat_id=-1010, message_id=111)
        bot.delete_message.assert_any_await(chat_id=-1010, message_id=112)
        assert await cards.obsolete(-1010, 111)
        assert await cards.obsolete(-1010, 112)
        message.edit_text.assert_not_awaited()

    asyncio.run(run())


def test_order_refresh_preserves_legacy_edit_when_flag_off(monkeypatch):
    from app.bot import telegram_bot

    monkeypatch.setenv("OMS_TELEGRAM_DURABLE_CARDS_ENABLED", "false")
    bot = SimpleNamespace(send_message=AsyncMock())
    message = SimpleNamespace(
        bot=bot, chat=SimpleNamespace(id=-1010), edit_text=AsyncMock()
    )
    assert asyncio.run(telegram_bot.safe_edit_message(
        message, "Старый режим", draft_id=0, order_id=8,
    ))
    message.edit_text.assert_awaited_once_with("Старый режим", reply_markup=None)
    bot.send_message.assert_not_awaited()
