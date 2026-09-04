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
    authorize = AsyncMock(return_value=True)
    with (
        patch.object(
            telegram_bot.manager_repository,
            "is_active_by_telegram_id",
            authorize,
        ),
        patch.object(telegram_bot, "DraftOrderService", return_value=service),
    ):
        asyncio.run(telegram_bot.draft_callback_handler(callback))

    service.set_customer_type.assert_not_awaited()
    authorize.assert_not_awaited()


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
        patch.object(
            telegram_bot.manager_repository,
            "is_active_by_telegram_id",
            AsyncMock(return_value=True),
        ),
        patch.object(telegram_bot, "DraftOrderService", return_value=service),
    ):
        asyncio.run(telegram_bot.draft_callback_handler(callback))

    assert events == ["ack", "mutation"]
    service.set_customer_type.assert_awaited_once()


def test_callback_is_acknowledged_before_async_manager_lookup():
    from app.bot import telegram_bot

    events = []

    async def authorize(_telegram_id):
        events.append("auth_started")
        await asyncio.sleep(0)
        events.append("auth_completed")
        return True

    callback = callback_with(AsyncMock(side_effect=lambda: events.append("ack")))
    service = SimpleNamespace(
        set_customer_type=AsyncMock(side_effect=lambda *_: events.append("action"))
    )
    with (
        patch.object(
            telegram_bot.manager_repository,
            "is_active_by_telegram_id",
            side_effect=authorize,
        ),
        patch.object(telegram_bot, "DraftOrderService", return_value=service),
    ):
        asyncio.run(telegram_bot.draft_callback_handler(callback))

    assert events == ["ack", "auth_started", "auth_completed", "action"]


def _assert_unauthorized_callback_is_blocked():
    from app.bot import telegram_bot

    callback = callback_with(AsyncMock())
    service = SimpleNamespace(set_customer_type=AsyncMock())
    with (
        patch.object(
            telegram_bot.manager_repository,
            "is_active_by_telegram_id",
            AsyncMock(return_value=False),
        ) as authorize,
        patch.object(telegram_bot, "DraftOrderService", return_value=service),
    ):
        asyncio.run(telegram_bot.draft_callback_handler(callback))

    callback.answer.assert_awaited_once_with()
    authorize.assert_awaited_once_with(898019732)
    service.set_customer_type.assert_not_awaited()


def test_inactive_manager_is_blocked():
    _assert_unauthorized_callback_is_blocked()


def test_unknown_manager_is_blocked():
    _assert_unauthorized_callback_is_blocked()


def test_unexpected_telegram_bad_request_is_not_swallowed():
    from app.bot import telegram_bot

    callback = callback_with(AsyncMock(side_effect=telegram_bad_request(
        "Bad Request: chat not found"
    )))
    with patch.object(
        telegram_bot.manager_repository,
        "is_active_by_telegram_id",
        AsyncMock(return_value=True),
    ):
        with pytest.raises(TelegramBadRequest, match="chat not found"):
            asyncio.run(telegram_bot.draft_callback_handler(callback))


def editable_callback(message):
    callback = callback_with(AsyncMock())
    callback.data = "draft:reject:2"
    callback.message = message
    return callback


def rejected_draft():
    return SimpleNamespace(id=2, status="rejected")


def test_identical_card_edit_is_successful_and_action_is_not_repeated():
    from app.bot import telegram_bot

    message = SimpleNamespace(edit_text=AsyncMock(side_effect=telegram_bad_request(
        "Bad Request: message is not modified"
    )))
    callback = editable_callback(message)
    service = SimpleNamespace(reject=AsyncMock(return_value=rejected_draft()))
    with (
        patch.object(
            telegram_bot.manager_repository,
            "is_active_by_telegram_id",
            AsyncMock(return_value=True),
        ),
        patch.object(telegram_bot, "DraftOrderService", return_value=service),
        patch.object(telegram_bot, "build_draft_card", return_value="same card"),
        patch.object(telegram_bot, "build_draft_keyboard", return_value={
            "inline_keyboard": []
        }),
    ):
        asyncio.run(telegram_bot.draft_callback_handler(callback))

    service.reject.assert_awaited_once_with(2)
    message.edit_text.assert_awaited_once()


def test_unexpected_card_edit_error_is_not_swallowed():
    from app.bot import telegram_bot

    message = SimpleNamespace(edit_text=AsyncMock(side_effect=telegram_bad_request(
        "Bad Request: message to edit not found"
    )))
    callback = editable_callback(message)
    service = SimpleNamespace(reject=AsyncMock(return_value=rejected_draft()))
    with (
        patch.object(
            telegram_bot.manager_repository,
            "is_active_by_telegram_id",
            AsyncMock(return_value=True),
        ),
        patch.object(telegram_bot, "DraftOrderService", return_value=service),
        patch.object(telegram_bot, "build_draft_card", return_value="new card"),
        patch.object(telegram_bot, "build_draft_keyboard", return_value={
            "inline_keyboard": []
        }),
    ):
        with pytest.raises(TelegramBadRequest, match="message to edit not found"):
            asyncio.run(telegram_bot.draft_callback_handler(callback))


def test_changed_card_is_edited_normally():
    from app.bot import telegram_bot

    message = SimpleNamespace(edit_text=AsyncMock())
    callback = editable_callback(message)
    service = SimpleNamespace(reject=AsyncMock(return_value=rejected_draft()))
    with (
        patch.object(
            telegram_bot.manager_repository,
            "is_active_by_telegram_id",
            AsyncMock(return_value=True),
        ),
        patch.object(telegram_bot, "DraftOrderService", return_value=service),
        patch.object(telegram_bot, "build_draft_card", return_value="changed card"),
        patch.object(telegram_bot, "build_draft_keyboard", return_value={
            "inline_keyboard": []
        }),
    ):
        asyncio.run(telegram_bot.draft_callback_handler(callback))

    message.edit_text.assert_awaited_once()
