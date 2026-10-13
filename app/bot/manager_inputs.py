"""Explicit replies only, bound to actor, chat and bot prompt; ordinary chat is untouched."""
import time
from aiogram.types import ForceReply, ReplyParameters, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest

pending = {}


def waiting(message):
    state = pending.get((message.chat.id, message.from_user.id))
    return bool(state and state[0] > time.monotonic() and message.reply_to_message
                and message.reply_to_message.message_id == state[1])


async def prompt_input(message, action, data, text):
    from html import escape
    sent = await message.answer(f'<a href="tg://user?id={message.from_user.id}">Менеджер</a>, {escape(text)}',
        parse_mode="HTML", reply_markup=ForceReply(selective=True),
        reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=False))
    pending[(message.chat.id, message.from_user.id)] = (time.monotonic() + 600, sent.message_id, action, data)


async def complete_source_prompt(message, data, prompt_id, label, row):
    """Reuse the original order card; discard the temporary ForceReply prompt."""
    markup = InlineKeyboardMarkup(inline_keyboard=[row])
    origin_id = data.get("origin_message_id")
    if origin_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id, message_id=origin_id, text=label,
                parse_mode=None, reply_markup=markup)
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error).casefold():
                # The old card may have been deleted; preserve the confirmed input.
                await message.answer(label, reply_markup=markup, parse_mode=None)
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=prompt_id)
        except TelegramBadRequest:
            pass  # Deletion is optional if Telegram permissions do not allow it.
    else:
        await message.answer(label, reply_markup=markup, parse_mode=None)


async def route(message, check_access):
    from app.bot.manager_menu import MENU_TEXTS
    if message.text and (message.text.startswith("/") or message.text in MENU_TEXTS):
        pending.pop((message.chat.id, message.from_user.id), None)
        return False
    if not waiting(message):
        return False
    if not await check_access(message):
        return True
    key = (message.chat.id, message.from_user.id)
    _, _, action, data = pending[key]
    if not message.text:
        await message.answer("Ответьте текстом.")
        return True
    try:
        if action == "reply":
            from app.services.order_desk import DeskService
            await DeskService().reply(data["draft_id"], message.from_user.id, message.chat.id, message.message_id, message.text)
            await message.answer("Сообщение передано клиенту.")
        elif action == "source_quantity":
            if not message.text.isascii() or not message.text.isdecimal() or not 0 < int(message.text) <= data["qty"]:
                await message.answer(f"Введите количество от 1 до {data['qty']}.")
                return True
            from app.bot.source_navigation import button
            row = button(f"Далее: {int(message.text)} шт.", dict(data, qty=int(message.text), step=data.get("quantity_next", "apply"), action="assign"))
            await complete_source_prompt(message, data, pending[key][1],
                                         "Проверьте количество для заказа №"
                                         + str(data["order_id"]), row)
        elif action == "source_cost":
            from decimal import Decimal, InvalidOperation
            try:
                price = Decimal(message.text.replace(",", "."))
                if not price.is_finite() or price < 0 or price * 100 != (price * 100).to_integral_value() or price > 1_000_000_000:
                    raise ValueError()
            except (InvalidOperation, ValueError):
                await message.answer("Введите цену в рублях, например 1250,50.")
                return True
            from app.bot.source_navigation import button
            row = button(f"Подтвердить: {data['qty']} шт. по {price} ₽", dict(data, base_cost_minor=int(price * 100), step="apply", action="assign"))
            await complete_source_prompt(message, data, pending[key][1],
                                         "Склад Х · заказ №" + str(data["order_id"])
                                         + "\nЗакупочная цена за единицу: " + str(price) + " ₽"
                                         + "\nКоличество: " + str(data["qty"]) + " шт."
                                         + "\nЭто ещё не создание закупки или документа МойСклад.",
                                         row)
        pending.pop(key, None)
    except ValueError as error:
        from app.services.order_desk import DeskError
        await message.answer(str(error) if isinstance(error, DeskError) else "Проверьте данные и повторите ответ.")
    return True
