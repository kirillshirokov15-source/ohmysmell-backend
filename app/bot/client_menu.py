"""Customer navigation, with order choices scoped to the verified Telegram user."""
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def client_keyboard(order_ids=(), *, intake=False):
    rows = [[KeyboardButton(text=identifier if isinstance(identifier, str) else f"Заявка №{identifier}")] for identifier in order_ids]
    rows += [[KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="🔎 Статус заказа")],
             [KeyboardButton(text="💬 Написать менеджеру")]]
    if intake == "lookup":
        rows.append([KeyboardButton(text="Новая заявка")])
    if intake == "contact":
        rows.append([KeyboardButton(text="Мой контакт", request_contact=True)])
    if intake == "confirm":
        rows.append([KeyboardButton(text="Отправить заявку")])
    if intake in {"items", "contact", "confirm"}:
        rows.append([KeyboardButton(text="Отмена")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


async def delivery_keyboard(session, actor):
    from sqlalchemy import select
    from app.models.operations import ClientConversation
    from app.models.order_desk import OrderDesk
    conversation = await session.get(ClientConversation, actor)
    identifiers = []
    if conversation and conversation.state in {"choose_status", "choose_message"}:
        desks = (await session.scalars(select(OrderDesk).where(
            OrderDesk.customer_telegram_id == actor).order_by(OrderDesk.draft_id.desc()).limit(10))).all()
        identifiers = [f"Заказ №{d.order_id}" if d.order_id else f"Заявка №{d.draft_id}" for d in desks]
    return client_keyboard(identifiers)
