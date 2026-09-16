"""Client dispatcher is constructed separately; no manager router is imported."""
import logging
from aiogram import Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from app.services.client_channel import ClientChannel
from app.logging_utils import log_event

logger = logging.getLogger(__name__)


def create_dispatcher(service=None):
    dp = Dispatcher()
    channel = service or ClientChannel()

    @dp.callback_query()
    async def no_manager_callbacks(callback):
        await callback.answer("Действие недоступно в клиентском боте.", show_alert=True)

    @dp.message()
    async def message(message):
        if message.chat.type != "private" or not message.from_user:
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
            log_event(logger, "client_request_failed", result=type(error).__name__)
            await message.answer("Сервис временно недоступен. Повторите сообщение; /status НОМЕР проверит уже отправленную заявку.")
    return dp
