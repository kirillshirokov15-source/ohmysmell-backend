"""Client dispatcher is constructed separately; no manager router is imported."""
import logging
from aiogram import Dispatcher, Router
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from app.services.client_channel import ClientChannel
from app.logging_utils import log_event

logger = logging.getLogger(__name__)


def create_dispatcher(service=None):
    dp = Dispatcher()
    router = Router(name="client")
    dp.include_router(router)
    channel = service or ClientChannel()

    @router.callback_query()
    async def no_manager_callbacks(callback):
        from aiogram.exceptions import TelegramBadRequest
        try:
            await callback.answer("Действие недоступно в клиентском боте.", show_alert=True)
        except TelegramBadRequest:
            # A stale, non-business callback must not stall durable polling.
            pass

    @router.message()
    async def message(message):
        if message.chat.type != "private" or not message.from_user:
            return
        import os
        if os.getenv("CLIENT_ORDER_DESK_ENABLED", "false").lower() == "true":
            from app.services.order_desk import DeskService
            # A failed DB commit must fail the update; never acknowledge a lost message.
            await DeskService().client(message.from_user.id, message.message_id, message.text)
            return
        phone = None
        if message.contact:
            if message.contact.user_id != message.from_user.id:
                await message.answer("Отправьте свой контакт или введите контакт текстом.")
                return
            phone = message.contact.phone_number
        try:
            reply = await channel.handle(message.from_user.id, message.message_id, message.text or "Контакт",
                name=message.from_user.full_name, verified_phone=phone)
            await message.answer(reply, reply_markup=ReplyKeyboardMarkup(keyboard=[
                [KeyboardButton(text="Мой контакт", request_contact=True)]], resize_keyboard=True))
        except Exception as error:
            log_event(logger, "client_request_failed", level=logging.ERROR, result=type(error).__name__)
            await message.answer("Сервис временно недоступен. Повторите сообщение; /status НОМЕР проверит уже отправленную заявку.")
    return dp
