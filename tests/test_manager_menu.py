import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, Update, User, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove

from tests.telegram_transport import TelegramSession


@pytest.fixture
def dispatch(monkeypatch):
    from app.bot import telegram_bot, procurement, order_sources
    monkeypatch.delenv("MANAGER_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("MANAGER_TELEGRAM_USER_IDS", raising=False)
    access = AsyncMock(return_value=True)
    orders = AsyncMock(return_value=[])
    purchases = AsyncMock()
    sources = Mock()
    monkeypatch.setattr(telegram_bot.manager_repository, "is_active_by_telegram_id", access)
    monkeypatch.setattr(telegram_bot, "list_orders", orders)
    monkeypatch.setattr(procurement, "show_procurements", purchases)
    monkeypatch.setattr(order_sources, "OrderSourceService", sources)

    def send(text, *, group=False, actor=11, chat_id=None, anonymous=False):
        session = TelegramSession()
        bot = Bot("123456:TEST_TOKEN", session=session)
        chat = Chat(id=chat_id or (-100123 if group else actor), type="supergroup" if group else "private")
        message = Message(message_id=42, date=datetime.now(timezone.utc), chat=chat,
                          from_user=User(id=actor, is_bot=False, first_name="Manager"),
                          sender_chat=chat if anonymous else None, text=text)
        async def run():
            try:
                await telegram_bot.dp.feed_update(bot, Update(update_id=1, message=message))
            finally:
                await bot.session.close()
        asyncio.run(run())
        return session.calls

    send.access, send.orders, send.purchases, send.sources = access, orders, purchases, sources
    return send


@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("text,hidden", [("/start", False), ("/menu", False), ("/hide_menu", True), ("Скрыть меню", True)])
def test_menu_target_and_hide(dispatch, monkeypatch, group, text, hidden):
    if group:
        monkeypatch.setenv("MANAGER_TELEGRAM_CHAT_ID", "-100123")
        monkeypatch.setenv("MANAGER_TELEGRAM_USER_IDS", "11")
    calls = dispatch(text, group=group)
    assert len(calls) == 1
    assert isinstance(calls[0].reply_markup, ReplyKeyboardRemove if hidden else ReplyKeyboardMarkup)
    assert calls[0].reply_markup.selective is group
    if group:
        assert calls[0].reply_parameters.message_id == 42
        assert calls[0].reply_parameters.allow_sending_without_reply is False
    else:
        assert calls[0].reply_parameters is None
    dispatch.access.assert_awaited_once_with(11)


@pytest.mark.parametrize("text,category", [("Заказы", "all"), ("Обновить", "all"), ("Отгрузки", "shipped"), ("/orders", "all")])
def test_order_alias_dispatches_once_and_keeps_inline(dispatch, text, category):
    calls = dispatch(text)
    dispatch.orders.assert_awaited_once_with(limit=10, offset=0, category=category)
    assert len(calls) == 1
    assert isinstance(calls[0].reply_markup, InlineKeyboardMarkup)


@pytest.mark.parametrize("text", ["Закупки", "/procurements"])
def test_procurements_alias(dispatch, text):
    dispatch(text)
    dispatch.purchases.assert_awaited_once()


@pytest.mark.parametrize("text,command", [("Источники", "/sources"), ("Найти заказ", "/order")])
def test_id_required_without_mutation(dispatch, text, command):
    calls = dispatch(text)
    assert len(calls) == 1 and command in calls[0].text
    dispatch.sources.assert_not_called()
    dispatch.orders.assert_not_awaited()


@pytest.mark.parametrize("text", ["Заказы", "Найти заказ", "Источники", "Отгрузки", "Закупки", "Обновить", "/menu", "Скрыть меню"])
def test_inactive_manager_cannot_use_menu(dispatch, text):
    dispatch.access.return_value = False
    calls = dispatch(text)
    assert len(calls) == 1 and calls[0].reply_markup is None
    dispatch.orders.assert_not_awaited()
    dispatch.purchases.assert_not_awaited()
    dispatch.sources.assert_not_called()


def test_group_guards_and_normal_conversation(dispatch, monkeypatch):
    monkeypatch.setenv("MANAGER_TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setenv("MANAGER_TELEGRAM_USER_IDS", "11")
    for kwargs in ({"actor": 12}, {"chat_id": -100124}, {"anonymous": True}):
        assert dispatch("Заказы", group=True, **kwargs) == []
    assert dispatch("Заказы завтра", group=True) == []
    assert dispatch("Заказы") == []  # Private chat is forbidden in group mode.
    dispatch.access.assert_not_awaited()
    calls = dispatch("Заказы", group=True)
    assert len(calls) == 1
    dispatch.orders.assert_awaited_once()


def test_ordinary_private_message_unchanged(dispatch):
    assert dispatch("Обсуждаем заказ с коллегой") == []
    dispatch.access.assert_not_awaited()
