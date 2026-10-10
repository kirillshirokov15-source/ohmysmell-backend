"""Presentation only; buttons are aliases of existing manager handlers."""
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, ReplyParameters

ROWS = (("Заказы", "Найти заказ"), ("Источники", "Отгрузки"),
        ("Закупки", "Обновить"), ("Скрыть меню",))
MENU_TEXTS = frozenset(text for row in ROWS for text in row)


def menu_options(message, *, hide=False):
    selective = message.chat.type in {"group", "supergroup"}
    markup = (ReplyKeyboardRemove(selective=selective) if hide else ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in ROWS],
        resize_keyboard=True, selective=selective))
    options = {"reply_markup": markup}
    if selective:
        # Telegram targets a selective keyboard to the author of the replied message.
        # No fallback to a non-reply: that could display the keyboard to the group.
        options["reply_parameters"] = ReplyParameters(message_id=message.message_id,
                                                       allow_sending_without_reply=False)
    return options
