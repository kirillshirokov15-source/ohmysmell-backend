import os

import requests
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

    for manager in managers:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": manager.telegram_id,
                "text": message,
            },
            timeout=20,
        )

        response.raise_for_status()
