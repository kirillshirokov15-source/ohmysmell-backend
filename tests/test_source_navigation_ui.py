"""Telegram UX: one live card and no orphaned "deleted message" quotes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage

from app.bot.source_navigation import replace_screen, source_warehouses
from app.bot.manager_inputs import complete_source_prompt


def test_navigation_edits_current_message_and_does_not_append():
    message = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    rows = [[{"text": "Назад", "callback_data": "order:refresh:5:0"}]]
    asyncio.run(replace_screen(message, "Новое состояние", rows))
    message.edit_text.assert_awaited_once()
    message.answer.assert_not_awaited()


def test_warehouse_x_never_duplicates_own_stock_source():
    catalog_entry = {"stocks": [
        {"id": "own-id", "name": "Основной склад 3143"},
        {"id": "x-id", "name": "Склад Х"},
    ]}
    allowed = ("own-id", "x-id")
    assert [s["id"] for s in source_warehouses(catalog_entry, allowed, "own")] == ["own-id"]
    assert [s["id"] for s in source_warehouses(catalog_entry, allowed, "partner_x")] == ["x-id"]
    assert [s["id"] for s in source_warehouses(catalog_entry, allowed, "external")] == ["own-id"]
    assert source_warehouses(catalog_entry, ("own-id",), "partner_x") == []


def test_price_reply_deletes_manager_reply_then_temporary_prompt():
    bot = SimpleNamespace(edit_message_text=AsyncMock(), delete_message=AsyncMock())
    message = SimpleNamespace(chat=SimpleNamespace(id=-100123456),
                              bot=bot, message_id=120, answer=AsyncMock())
    data = {"origin_message_id": 118, "order_id": 5}
    row = [{"text": "Подтвердить X", "callback_data": "sourceui:fake-test-token"}]
    asyncio.run(complete_source_prompt(message, data, 119, "Цена X подтверждается", row))
    bot.edit_message_text.assert_awaited_once()
    assert [x.kwargs["message_id"] for x in bot.delete_message.await_args_list] == [120, 119]
    message.answer.assert_not_awaited()


def test_if_user_reply_cannot_be_deleted_keep_bot_prompt_alive():
    err = TelegramBadRequest(method=DeleteMessage(chat_id=-100123456, message_id=120),
                             message="Bad Request: message can't be deleted")
    bot = SimpleNamespace(edit_message_text=AsyncMock(),
                          delete_message=AsyncMock(side_effect=err))
    message = SimpleNamespace(chat=SimpleNamespace(id=-100123456),
                              bot=bot, message_id=120, answer=AsyncMock())
    data = {"origin_message_id": 118, "order_id": 5}
    row = [{"text": "Подтвердить X", "callback_data": "sourceui:fake-test-token"}]
    asyncio.run(complete_source_prompt(message, data, 119, "Цена X подтверждается", row))
    bot.delete_message.assert_awaited_once()
    assert bot.delete_message.await_args.kwargs["message_id"] == 120
    assert bot.edit_message_text.await_count == 2  # original card + completed prompt
    message.answer.assert_not_awaited()


def test_optional_x_cost_keypad_is_in_card_and_not_a_chat_prompt():
    from app.bot.source_navigation import x_cost_input, x_cost_keypad, update_x_cost_digits
    base = dict(order_id=5, allocation_id=10, revision=2,
                actor=111, chat=-1001, step="cost", cost_digits="")
    assert x_cost_input("") is None
    assert x_cost_input("2000") == 200000
    assert x_cost_input("2000,50") == 200050
    assert x_cost_input("2000,501") is None
    assert x_cost_input("1000000001") is None
    assert update_x_cost_digits("2000", ",") == "2000,"
    assert update_x_cost_digits("2000,5", "0") == "2000,50"
    assert update_x_cost_digits("2000,50", "1") == "2000,50"
    assert update_x_cost_digits("2000,50", "⌫") == "2000,5"
    assert update_x_cost_digits("2000", "clear") == ""
    title, buttons = x_cost_keypad({**base, "cost_digits": "2000"})
    assert "2000 ₽" in title
    assert "Tilda" in title
    assert any(b["text"].startswith("Далее") for row in buttons for b in row)
    title, buttons = x_cost_keypad(base)
    assert not any(b["text"].startswith("Далее") for row in buttons for b in row)
