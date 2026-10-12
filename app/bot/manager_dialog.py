"""Short-lived, actor-scoped navigation; no order mutations."""
import time
from aiogram.types import ForceReply, InlineKeyboardMarkup, InlineKeyboardButton, ReplyParameters

TTL = 180
pending = {}
BUTTONS = {"Найти заказ": "order", "🔎 Найти заказ": "order",
           "Источники": "sources", "🏬 Источники": "sources"}


def key(message):
    return message.chat.id, message.from_user.id


def waiting(message):
    now = time.monotonic()
    for owner, state in list(pending.items()):
        if state[2] <= now:
            pending.pop(owner, None)
    state = pending.get(key(message))
    if not state:
        return False
    if message.chat.type in {"group", "supergroup"}:
        reply = message.reply_to_message
        return bool(reply and reply.message_id == state[1])
    return True


def fallback(actor, *, more=False):
    rows = ((('Закупки', 'procurements'), ('Отгрузки', 'shipped')),
            (('Заявки', 'drafts'), ('Справка', 'help')),
            (('Назад', 'home'),)) if more else (
            (('📦 Заказы', 'orders'), ('🔎 Найти заказ', 'order')), (('☰ Ещё', 'more'),))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"menu:{actor}:{action}") for label, action in row]
        for row in rows])


async def prompt(message, action):
    # Mention targets ForceReply even when navigation originated in a callback.
    result = await message.answer(
        f'<a href="tg://user?id={message.from_user.id}">Менеджер</a>, введите номер заказа (только число). '
        'Ответьте на это сообщение.',
        parse_mode="HTML", reply_markup=ForceReply(selective=True),
        reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=False))
    pending[key(message)] = (action, result.message_id, time.monotonic() + TTL)


async def route(message, check_access, handlers):
    text = message.text or ""
    command = text.split()[0].split('@')[0] if text else ""
    action = BUTTONS.get(text)
    matched = waiting(message)
    expected = pending.get(key(message))
    from app.bot.manager_menu import MENU_TEXTS
    navigation = key(message) in pending and (command.startswith('/') or text in MENU_TEXTS)
    if not (action or command == '/cancel' or matched or navigation):
        return False
    if not await check_access(message):
        pending.pop(key(message), None)
        return True
    if command.startswith('/'):
        pending.pop(key(message), None)
        if command == '/cancel':
            await message.answer('Ожидание номера отменено.')
            return True
        return False
    if action:
        await prompt(message, action)
        return True
    if navigation:
        pending.pop(key(message), None)
        return False
    if pending.get(key(message)) != expected or not waiting(message):
        return True  # Another update already consumed/replaced/expired this prompt.
    if not text.isascii() or not text.isdecimal() or not (0 < len(text) <= 10) or not (0 < int(text) <= 2147483647):
        await message.answer('Нужен положительный номер заказа. Ответьте числом на запрос бота или /cancel.')
        return True
    action, _, _ = pending.pop(key(message))
    # Reuse the command handler with the original actor/chat/message identity.
    forwarded = message.model_copy(update={"text": f"/{action} {int(text)}"}).as_(message.bot)
    await handlers[action](forwarded)
    return True
