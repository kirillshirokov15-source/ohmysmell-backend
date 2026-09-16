"""Aiogram transport contract fake: real Updates/dispatch/TelegramMethod models."""
from datetime import datetime, timezone
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, EditMessageText, AnswerCallbackQuery, GetMe
from aiogram.types import Message, Chat, User


class TelegramSession(BaseSession):
    def __init__(self, failure=None):
        super().__init__()
        self.calls = []
        self.failure = failure
        self.closed = False

    async def close(self):
        self.closed = True

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if self.failure:
            error, self.failure = self.failure, None
            raise error
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"), text=method.text)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, GetMe):
            return User(id=bot.id, is_bot=True, first_name="Fake")
        raise AssertionError("Unhandled Telegram method " + type(method).__name__)

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("No file downloads in this contract")
        yield b""
