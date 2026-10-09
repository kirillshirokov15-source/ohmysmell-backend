import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

from tests.telegram_transport import TelegramSession


@pytest.fixture
def manager_bot(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:unit-test-token")
    monkeypatch.setenv("MANAGER_TELEGRAM_MODE", "auto")
    monkeypatch.delenv("MANAGER_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("MANAGER_TELEGRAM_USER_IDS", raising=False)
    from app.bot import telegram_bot
    from app.config.settings import settings

    monkeypatch.setattr(settings, "environment", "staging")
    lookup = AsyncMock(side_effect=AssertionError("Unexpected manager lookup"))
    monkeypatch.setattr(telegram_bot.manager_repository, "is_active_by_telegram_id", lookup)
    return telegram_bot


def dispatch(manager_bot, text="/chatid", **overrides):
    transport = TelegramSession()
    bot = Bot("123456:unit-test-token", session=transport)
    bot._me = User(id=bot.id, is_bot=True, first_name="Test", username="manager_test_bot")
    data = dict(message_id=1, date=datetime.now(timezone.utc),
                chat=Chat(id=-100123, type="supergroup"),
                from_user=User(id=42, is_bot=False, first_name="Test"), text=text)
    data.update(overrides)
    update = Update(update_id=1, message=Message(**data))
    asyncio.run(manager_bot.dp.feed_update(bot, update))
    return [call for call in transport.calls if isinstance(call, SendMessage)]


@pytest.mark.parametrize("text", ["/chatid", "/chatid@manager_test_bot"])
@pytest.mark.parametrize("configured", [False, True])
def test_chatid_before_or_outside_allowlist(manager_bot, monkeypatch, text, configured):
    from app.bot.manager_group import validate_group_config

    if configured:
        monkeypatch.setenv("MANAGER_TELEGRAM_CHAT_ID", "-100999")
        monkeypatch.setenv("MANAGER_TELEGRAM_USER_IDS", "99")
    assert validate_group_config() == ("group" if configured else "private")
    calls = dispatch(manager_bot, text)
    assert len(calls) == 1
    assert calls[0].text == "chat_id=-100123\nuser_id=42"
    assert calls[0].chat_id == -100123
    assert calls[0].parse_mode is None


@pytest.mark.parametrize("environment", ["production", "development", ""])
def test_chatid_staging_only(manager_bot, monkeypatch, environment):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "environment", environment)
    assert dispatch(manager_bot) == []


@pytest.mark.parametrize("text", ["chatid", "/chatids", "hello /chatid", "/chatid@another_bot",
                                 "/drafts", "/orders", "/start", "/quantity 1 2 3"])
def test_other_messages_remain_blocked(manager_bot, text):
    assert dispatch(manager_bot, text) == []


@pytest.mark.parametrize("overrides", [
    {"from_user": None},
    {"sender_chat": Chat(id=-100123, type="supergroup")},
    {"text": None, "caption": "/chatid"},
])
def test_no_diagnostic_for_anonymous_sender_or_caption(manager_bot, overrides):
    assert dispatch(manager_bot, **overrides) == []


def test_diagnostic_does_not_authorize_group_actions(manager_bot, monkeypatch):
    from app.bot.manager_group import allowed_event

    monkeypatch.setenv("MANAGER_TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setenv("MANAGER_TELEGRAM_USER_IDS", "99")
    assert dispatch(manager_bot)[0].text == "chat_id=-100123\nuser_id=42"
    assert dispatch(manager_bot, "/orders") == []
    message = Message(message_id=1, date=datetime.now(timezone.utc),
                      chat=Chat(id=-100123, type="supergroup"), text="/orders",
                      from_user=User(id=42, is_bot=False, first_name="Test"))
    assert not allowed_event(message)
    from types import SimpleNamespace
    assert not allowed_event(SimpleNamespace(message=message, from_user=message.from_user), callback=True)
