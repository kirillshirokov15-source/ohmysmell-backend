"""Durable group events, at-least-once delivery; no private/group duplication."""
import asyncio
import os
from sqlalchemy import select
from app.models.buying import BuyingEvent, BuyingPurchase
from app.database.session import async_session
from app.services.buying import now
from app.bot.manager_group import group_id, notification_chats
from app.integrations.http_tls import verified_session

LABELS = {"created": "Новая закупка", "email_simulated": "Письмо поставщику: симуляция", "received": "Товар получен", "supplier_reply": "Ответ поставщика"}


async def send_group(text):
    chats = notification_chats([])
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token or not chats:
        raise ValueError("Manager group notification is not configured")
    def send():
        with verified_session() as http:
            response = http.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chats[0], "text": text}, timeout=(5,20))
            if not response.ok:
                raise ValueError("Telegram group notification failed")
    await asyncio.to_thread(send)


LABELS.update(email_sent='Письмо поставщику отправлено', procurement_error='Ошибка закупки')


async def run_once(sender=send_group):
    if group_id() is None:
        return False
    async with async_session() as session, session.begin():
        event = await session.scalar(select(BuyingEvent).where(BuyingEvent.notified_at.is_(None)).order_by(BuyingEvent.id).limit(1).with_for_update(skip_locked=True))
        if not event:
            return False
        purchase = await session.get(BuyingPurchase, event.purchase_id)
        supplier = purchase.snapshot.get('supplier_name', '') if purchase else ''
        actor = (purchase.received_by_username or event.actor) if purchase and event.action == 'received' else event.actor
        await sender(f"Buying B-{event.purchase_id:06d}: {LABELS.get(event.action,event.action)} · {supplier} · {actor} · событие #{event.id}")
        event.notified_at = now()
    return True
