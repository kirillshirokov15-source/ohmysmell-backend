"""Manager read models: cards never need provider requests."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database.session import async_session
from app.models.fulfillment import Shipment
from app.services.telegram_display import telegram_rubles, customer_type_label

FULFILLMENT_LABELS = {"new": "Новый", "assembling": "В сборке", "assembled": "Собран", "shipped": "Отгружен", "cancelled": "Отменён"}
PAYMENT_LABELS = {"unpaid": "Не оплачен", "paid": "Оплачен"}
DELIVERY_LABELS = {"unselected": "Не выбран", "pickup": "Самовывоз", "manual": "Курьер", "cdek": "СДЭК", "yandex": "Яндекс"}
DELIVERY_STATUS = {"pending": "Ожидает", "ready": "Готова", "dispatched": "Передана", "delivered": "Доставлена", "cancelled": "Отменена"}
SOURCE_LABELS = {"telegram": "Telegram", "email": "Почта", "website": "Сайт", "manual": "Менеджер", "instagram": "Instagram"}


async def shipments_for(order_id):
    async with async_session() as session:
        return list((await session.execute(select(Shipment).where(Shipment.order_id == order_id)
            .options(selectinload(Shipment.allocations)).order_by(Shipment.warehouse_id))).scalars())


def order_card(order, shipments=()):
    lines = [f"Заказ №{order.id}", f"Клиент: {order.customer_name}", f"Тип: {customer_type_label(order.customer_type)}",
        f"Email: {getattr(order, 'customer_email', None) or 'не указан'}",
        f"Телефон: {order.phone or 'не указан'}", f"Telegram: {order.telegram or 'не указан'}",
        f"Источник: {SOURCE_LABELS.get(order.source, order.source)}",
        f"Контрагент: {order.counterparty_name or order.counterparty_id or 'не выбран'}",
        f"Сборка: {FULFILLMENT_LABELS[order.fulfillment_status]}", f"Оплата: {PAYMENT_LABELS[order.payment_status]}",
        f"Доставка: {DELIVERY_LABELS[order.delivery_method]} · {DELIVERY_STATUS[order.delivery_status]}"]
    if order.delivery_reference:
        lines.append(f"Номер доставки: {order.delivery_reference}")
    if order.needs_review:
        lines.append("⚠️ Требует проверки")
    if order.paid_at:
        lines.append(f"Оплата отмечена: {order.paid_at:%d.%m.%Y %H:%M} UTC · менеджер #{order.paid_by_manager_id}")
    if order.payment_note:
        lines.append(f"Примечание об оплате: {order.payment_note}")
    lines.append(f"Итого: {telegram_rubles(order.total)}")
    for item in order.items:
        lines.append(f"• {item.name[:150]} · {item.qty} × {telegram_rubles(item.price)} = {telegram_rubles(item.item_total)}")
    by_id = {i.id: i for i in order.items}
    for index, shipment in enumerate(shipments, 1):
        lines.append(f"\nСклад {index} ({shipment.warehouse_id}):")
        for allocation in shipment.allocations:
            item = by_id.get(allocation.order_item_id)
            lines.append(f"• {item.name[:100] if item else allocation.order_item_id}: {allocation.qty} шт.")
    if not shipments:
        lines.append("Распределение по складам ещё не выполнено.")
    lines.append(f"Версия: {order.revision}")
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3850] + f"\nВсе позиции и склады: /items {order.id}"


def order_keyboard(order, shipments=()):
    buttons = []
    def add(label, action):
        buttons.append([{"text": label, "callback_data": f"order:{action}:{order.id}:{order.revision}"}])
    if order.fulfillment_status != "cancelled" and order.status != "rejected":
        if order.needs_review:
            add("Проверка завершена", "resolve")
        else:
            if order.fulfillment_status == "new":
                add("Начать сборку" if shipments else "Распределить по складам", "assembling" if shipments else "allocate")
            elif order.fulfillment_status == "assembling":
                add("Заказ собран", "assembled")
            elif order.fulfillment_status == "assembled" and order.delivery_method != "unselected":
                add("Отгружен", "shipped")
            add("Проблема / требуется проверка", "review")
        if order.payment_status == "unpaid":
            add("Оплачен", "paid")
            if order.fulfillment_status != "shipped":
                add("Отменить заказ", "cancel_confirm")
        if order.fulfillment_status != "shipped":
            for method in ("pickup", "manual", "cdek", "yandex"):
                if method != order.delivery_method:
                    add(DELIVERY_LABELS[method], "delivery_" + method)
        elif order.delivery_status == "dispatched":
            add("Доставлен", "delivered")
    add("Обновить", "refresh")
    return {"inline_keyboard": buttons}
