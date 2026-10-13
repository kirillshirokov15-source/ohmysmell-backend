"""Manager navigation is single-message; no live Telegram or MoySklad calls."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot.source_navigation import replace_screen
from app.bot.manager_inputs import complete_source_prompt


def test_navigation_edits_current_message_and_does_not_append():
    message = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    rows = [[{"text": "Назад", "callback_data": "order:refresh:5:0"}]]
    asyncio.run(replace_screen(message, "Новое состояние", rows))
    message.edit_text.assert_awaited_once()
    message.answer.assert_not_awaited()


def test_price_reply_edits_original_card_and_deletes_temporary_prompt():
    bot = SimpleNamespace(edit_message_text=AsyncMock(), delete_message=AsyncMock())
    message = SimpleNamespace(chat=SimpleNamespace(id=-100123456),
                              bot=bot, answer=AsyncMock())
    data = {"origin_message_id": 118, "order_id": 5}
    row = [{"text": "Подтвердить X", "callback_data": "sourceui:fake-test-token"}]
    asyncio.run(complete_source_prompt(message, data, 119, "Цена X подтверждается", row))
    bot.edit_message_text.assert_awaited_once()
    bot.delete_message.assert_awaited_once_with(chat_id=-100123456, message_id=119)
    message.answer.assert_not_awaited()
