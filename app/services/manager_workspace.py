"""Manager read models: cards never need provider requests."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database.session import async_session
from app.models.fulfillment import Shipment, WarehouseAllocation
from app.services.telegram_display import telegram_rubles, customer_type_label

FULFILLMENT_LABELS = {"new": "Новый", "assembling": "В сборке", "assembled": "Собран", "shipped": "Отгружен", "cancelled": "Отменён"}
PAYMENT_LABELS = {"unpaid": "Не оплачен", "paid": "Оплачен"}
DELIVERY_LABELS = {"unselected": "Не выбран", "pickup": "Самовывоз", "manual": "Курьер", "cdek": "СДЭК", "yandex": "Яндекс"}
DELIVERY_STATUS = {"pending": "Ожидает", "ready": "Готова", "dispatched": "Передана", "delivered": "Доставлена", "cancelled": "Отменена"}
SOURCE_LABELS = {"telegram": "Telegram", "email": "Почта", "website": "Сайт", "manual": "Менеджер", "instagram": "Instagram"}


async def shipments_for(order_id):
    async with async_session() as session:
        return list((await session.execute(select(Shipment).where(Shipment.order_id == order_id)
            .options(selectinload(Shipment.allocations).selectinload(WarehouseAllocation.buying_purchase))
            .order_by(Shipment.warehouse_id))).scalars())


def order_card(order, shipments=()):
    from app.services.desk_status import stage_label
    lines = [f"Заказ №{order.id}", f"Статус: {stage_label(None, None, order)}", f"Клиент: {order.customer_name}", f"Тип: {customer_type_label(order.customer_type)}",
        f"Email: {getattr(order, 'customer_email', None) or 'не указан'}",
        f"Телефон: {order.phone or 'не указан'}", f"Telegram: {order.telegram or 'не указан'}",
        f"Источник: {SOURCE_LABELS.get(order.source, order.source)}",
        f"Сборка: {FULFILLMENT_LABELS[order.fulfillment_status]}", f"Оплата: {PAYMENT_LABELS[order.payment_status]}",
        f"Доставка: {DELIVERY_LABELS[order.delivery_method]} · {DELIVERY_STATUS[order.delivery_status]}"]
    if not getattr(order, "manual_fulfillment", False):
        lines.append(f"Контрагент: {order.counterparty_name or order.counterparty_id or 'не выбран'}")
    if order.delivery_reference:
        lines.append(f"Номер доставки: {order.delivery_reference}")
    if getattr(order, "assigned_manager_telegram_id", None):
        lines.append(f"Ответственный: Telegram ID {order.assigned_manager_telegram_id}")
    if order.needs_review:
        lines.append("⚠️ Требует проверки")
    if order.paid_at:
        lines.append(f"Оплата отмечена: {order.paid_at:%d.%m.%Y %H:%M} UTC · менеджер #{order.paid_by_manager_id}")
    if order.payment_note:
        lines.append(f"Примечание об оплате: {order.payment_note}")
    lines.append(f"Итого: {telegram_rubles(order.total)}")
    for item in order.items:
        lines.append(f"• #{item.id} {item.name[:150]} · {item.qty} × {telegram_rubles(item.price)} = {telegram_rubles(item.item_total)}")
    by_id = {i.id: i for i in order.items}
    for index, shipment in enumerate(shipments, 1):
        lines.append(f"\nСклад {index} ({shipment.warehouse_id}), план: {shipment.status}:")
        for allocation in shipment.allocations:
            item = by_id.get(allocation.order_item_id)
            lines.append(f"• {item.name[:100] if item else allocation.order_item_id}: {allocation.qty} шт.")
            if getattr(allocation, "managed_source", False):
                from app.services.order_source_service import allocation_ready
                from app.services.supply_service import LABELS
                purchase = allocation.buying_purchase
                status = {"awaiting_receipt": "ожидает получения", "received": "получено, требуется оприходование",
                    "confirmed": "источник подтверждён", "cancelled": "назначение отменено"}[allocation.supply_status]
                ready = shipment.status in {"planned", "exported"} and allocation_ready(allocation, manual=True)
                lines.append(f"  Назначение #{allocation.id}: {LABELS[allocation.source_type]} · {status} · "
                    f"{'доступно к сборке' if ready else 'недоступно к отгрузке'}")
                if purchase:
                    lines.append(f"  Buying B-{purchase.id:06d}: {purchase.status}" +
                        (" · требуется оформить закупку" if purchase.status == "draft" else ""))
                if allocation.receipt_simulated:
                    lines.append("  Получение/оприходование: симуляция, без записи МойСклад")
    if not shipments:
        lines.append("Назначьте источники: /sources НОМЕР_ЗАКАЗА. До подтверждения источников отгрузка заблокирована."
            if getattr(order, "manual_fulfillment", False) else "Распределение по складам ещё не выполнено.")
    text = "\n".join(lines)
    raw = text.encode("utf-16-le")
    return text if len(raw) <= 8000 else raw[:7600].decode("utf-16-le", errors="ignore") + f"\nВсе позиции и склады: /items {order.id}"


def order_keyboard(order, shipments=()):
    buttons = []
    def add(label, action):
        buttons.append([{"text": label, "callback_data": f"order:{action}:{order.id}:{order.revision}"}])
    if order.fulfillment_status != "cancelled" and order.status != "rejected":
        if order.needs_review:
            if not getattr(order, "supply_financial_review", False):
                add("Проверка завершена", "resolve")
        else:
            if order.fulfillment_status == "new":
                external_only = getattr(order, "supply_external_only", False)
                manual = getattr(order, "manual_fulfillment", False)
                if manual:
                    from app.services.order_source_service import OrderSourceService
                    view = OrderSourceService.view(order, shipments)
                    can_start = bool(view["items"]) and all(i["ready"] == i["qty"] for i in view["items"])
                else:
                    can_start = bool(shipments) or (external_only and getattr(order, "supply_received", False))
                waiting_supply = getattr(order, "supply_has_external", False) and not getattr(order, "supply_received", False)
                if not waiting_supply and (not external_only or can_start) and (not manual or can_start):
                    add("Начать сборку" if can_start else "Распределить по складам", "assembling" if can_start else "allocate")
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
    if getattr(order, "desk_draft_id", None):
        buttons.append([{"text": "Заявка сайта / связь с клиентом", "callback_data": f"desk:refresh:{order.desk_draft_id}"}])
    if getattr(order, "supply_has_external", False):
        add("Закупки по заказу", "procurement")
    return {"inline_keyboard": buttons}
