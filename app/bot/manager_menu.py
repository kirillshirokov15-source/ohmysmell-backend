"""Presentation only; buttons are aliases of existing manager handlers."""
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, ReplyParameters

ROWS = (("📦 Заказы", "🔎 Найти заказ"), ("☰ Ещё",))
MENU_TEXTS = frozenset(text for row in ROWS for text in row) | frozenset(
    {"Заказы", "Найти заказ", "Источники", "Отгрузки", "Закупки", "Обновить", "Скрыть меню"})


def menu_options(message, *, hide=False):
    selective = message.chat.type in {"group", "supergroup"}
    markup = (ReplyKeyboardRemove(selective=selective) if hide or selective else ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in ROWS],
        resize_keyboard=True, is_persistent=True, selective=selective))
    options = {"reply_markup": markup}
    if selective:
        # Telegram targets a selective keyboard to the author of the replied message.
        # No fallback to a non-reply: that could display the keyboard to the group.
        options["reply_parameters"] = ReplyParameters(message_id=message.message_id,
                                                       allow_sending_without_reply=False)
    return options


async def show_menu(message):
    if message.chat.type not in {"group", "supergroup"}:
        return await message.answer("Выберите действие", **menu_options(message))
    from app.bot.manager_dialog import fallback
    # Inline markup does not clear a client's old reply keyboard. Remove it for
    # the author of the command first, then attach inline buttons to THIS message.
    # Never delete history or send a second menu as an edit fallback.
    sent = await message.answer("Меню менеджера", **menu_options(message, hide=True))
    await message.bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=sent.message_id,
        reply_markup=fallback(message.from_user.id))
    return sent
