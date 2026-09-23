import os

import asyncio
from app.integrations.http_tls import verified_session
from sqlalchemy import select

from app.database.session import async_session
from app.models.manager import Manager
from app.services.money import format_rubles


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


async def notify_managers(order_id: int, order: dict) -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN не найден")
        return

    async with async_session() as session:
        result = await session.execute(
            select(Manager).where(
                Manager.is_active.is_(True)
            )
        )

        managers = result.scalars().all()

    items_text = "\n".join(
        f"• {item['name']} × {item['qty']} – {format_rubles(item['sum'])} ₽"
        for item in order["items"]
    )

    message = (
        f"🛒 Новый заказ #{order_id}\n\n"
        f"👤 {order['customer_name']}\n"
        f"📞 {order['phone']}\n"
        f"✈️ {order.get('telegram') or '–'}\n\n"
        f"{items_text}\n\n"
        f"💰 Итого: {format_rubles(order['total'])} ₽"
    )

    if order.get("comment"):
        message += f"\n\n💬 {order['comment']}"

    from app.bot.manager_group import notification_chats
    chat_ids = notification_chats(managers)
    def send():
        with verified_session() as http:
            for chat_id in chat_ids:
                response = http.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": message[:4000]}, timeout=(5, 20))
                if not response.ok:
                    raise RuntimeError(f"Telegram notification HTTP {response.status_code}")
    await asyncio.to_thread(send)
