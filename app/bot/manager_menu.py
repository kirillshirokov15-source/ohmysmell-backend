"""Presentation only; buttons are aliases of existing manager handlers."""
import asyncio
import logging
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, ReplyParameters
from app.bot.manager_ui_flags import enabled as modern_ui_enabled
from app.logging_utils import log_event

logger = logging.getLogger(__name__)
# Navigation only, scoped to this worker like manager_dialog.pending. No CRM state.
group_menus = {}


def bad_request_reason(error):
    text = error.message.casefold()
    for phrase, code in (
        ("message is not modified", "not_modified"),
        ("message to edit not found", "edit_not_found"),
        ("message can't be edited", "not_editable"),
        ("message to be replied not found", "reply_not_found"),
        ("reply message not found", "reply_not_found"),
        ("chat not found", "chat_not_found"),
        ("inline keyboard expected", "inline_keyboard_expected"),
    ):
        if phrase in text:
            return code
    return "other_bad_request"

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
    key = (message.bot.id, message.chat.id, message.message_thread_id, message.from_user.id)
    state = group_menus.setdefault(key, {"lock": asyncio.Lock(), "removed": False, "message_id": None})
    async with state["lock"]:
        if not state["removed"]:
            # Telegram cannot edit a message sent with a reply keyboard/remove.
            # Keep this addressed removal separate from the inline-only message.
            await message.answer("Старая клавиатура скрыта.", **menu_options(message, hide=True))
            state["removed"] = True
        if modern_ui_enabled():
            # Always bring navigation to the newest messages in the shared group.
            # Remove only the prior menu message created by this worker.
            sent = await message.answer(
                "🏠 Главное меню · выберите раздел",
                reply_markup=fallback(message.from_user.id),
                reply_parameters=ReplyParameters(
                    message_id=message.message_id,
                    allow_sending_without_reply=False,
                ),
            )
            previous = state["message_id"]
            state["message_id"] = sent.message_id
            if previous is not None and previous != sent.message_id:
                try:
                    await message.bot.delete_message(message.chat.id, previous)
                except TelegramBadRequest as error:
                    log_event(logger, "manager_menu_prior_delete_rejected",
                              result=bad_request_reason(error))
            return sent
        if state["message_id"] is not None:
            try:
                return await message.bot.edit_message_text(chat_id=message.chat.id,
                    message_id=state["message_id"], text="Меню менеджера", parse_mode=None,
                    reply_markup=fallback(message.from_user.id))
            except TelegramBadRequest as error:
                reason = bad_request_reason(error)
                log_event(logger, "manager_menu_edit_rejected", result=reason)
                if reason == "not_modified":
                    return
                if reason not in {"edit_not_found", "not_editable"}:
                    raise
                # Only a definite rejection allows a replacement. No timeout retry.
                state["message_id"] = None
        sent = await message.answer("Меню менеджера", reply_markup=fallback(message.from_user.id),
            reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=False))
        state["message_id"] = sent.message_id
        return sent
