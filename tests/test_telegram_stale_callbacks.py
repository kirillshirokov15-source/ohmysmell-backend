import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest


def callback_with(answer):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=898019732),
        data="draft:type:wholesale:2",
        answer=answer,
        message=None,
    )


def telegram_bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SimpleNamespace(), message=message)


def test_stale_callback_is_ignored_before_business_mutation():
    from app.bot import telegram_bot

    callback = callback_with(AsyncMock(side_effect=telegram_bad_request(
        "Bad Request: query is too old and response timeout expired"
    )))
    service = SimpleNamespace(set_customer_type=AsyncMock())
    with (
        patch.object(telegram_bot, "is_active_manager", return_value=True),
        patch.object(telegram_bot, "DraftOrderService", return_value=service),
    ):
        asyncio.run(telegram_bot.draft_callback_handler(callback))

    service.set_customer_type.assert_not_awaited()


def test_fresh_callback_is_acknowledged_before_service_call():
    from app.bot import telegram_bot

    events = []
    answer = AsyncMock(side_effect=lambda: events.append("ack"))
    callback = callback_with(answer)
    service = SimpleNamespace(
        set_customer_type=AsyncMock(
            side_effect=lambda *_: events.append("mutation") or SimpleNamespace(
                customer_type="wholesale"
            )
        )
    )
    with (
        patch.object(telegram_bot, "is_active_manager", return_value=True),
        patch.object(telegram_bot, "DraftOrderService", return_value=service),
    ):
        asyncio.run(telegram_bot.draft_callback_handler(callback))

    assert events == ["ack", "mutation"]
    service.set_customer_type.assert_awaited_once()


def test_unexpected_telegram_bad_request_is_not_swallowed():
    from app.bot import telegram_bot

    callback = callback_with(AsyncMock(side_effect=telegram_bad_request(
        "Bad Request: chat not found"
    )))
    with patch.object(telegram_bot, "is_active_manager", return_value=True):
        with pytest.raises(TelegramBadRequest, match="chat not found"):
            asyncio.run(telegram_bot.draft_callback_handler(callback))
