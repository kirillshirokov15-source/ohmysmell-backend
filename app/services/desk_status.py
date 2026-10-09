"""Read-only lifecycle projection. Order owns confirmed payment/fulfillment/delivery.

OrderDesk.stage is only pre-confirmation coordination, never a second Order status.
No historical rows or audit events need to be rewritten to repair old desk stages.
"""
from app.services.manager_workspace import FULFILLMENT_LABELS, PAYMENT_LABELS, DELIVERY_STATUS


def stage_label(desk, draft, order=None):
    if order is not None:
        if order.fulfillment_status == "cancelled" or order.status == "rejected":
            return "Отменён"
        if order.delivery_status == "delivered":
            return "Доставлен"
        if order.fulfillment_status != "new":
            return FULFILLMENT_LABELS[order.fulfillment_status]
        return "Оплачен" if order.payment_status == "paid" else "Ожидает оплаты"
    if desk.order_id:
        return "Статус уточняется у менеджера"
    if draft.status == "rejected":
        return "Отменена"
    return {"new": "Получен", "working": "В работе",
            "awaiting_confirmation": "Ожидает подтверждения",
            "awaiting_payment": "Ожидает подтверждения"}[desk.stage]


def public_summary(desk, draft, order=None):
    label = stage_label(desk, draft, order)
    if order is None:
        return f"Заявка №{desk.draft_id}: {label}"
    payment = PAYMENT_LABELS[order.payment_status]
    suffix = f". {payment}" if payment != label else ""
    return f"Заявка №{desk.draft_id}, заказ №{order.id}: {label}{suffix}."


def order_details(order):
    return [f"Оплата: {PAYMENT_LABELS[order.payment_status]}",
            f"Сборка: {FULFILLMENT_LABELS[order.fulfillment_status]}",
            f"Доставка: {DELIVERY_STATUS[order.delivery_status]}"]
