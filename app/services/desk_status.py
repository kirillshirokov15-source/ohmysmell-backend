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
    # Historical 'new'/'working' drafts also appear as awaiting approval.
    # This is a status projection, not a conversion into a confirmed order.
    return {"new": "Ожидает подтверждения", "working": "Ожидает подтверждения",
            "awaiting_confirmation": "Ожидает подтверждения",
            "awaiting_payment": "Ожидает подтверждения"}[desk.stage]



def tilda_product_lines(draft, *, max_utf16=2800):
    """Public Tilda checkout snapshot, not internal MoySklad product names.

    Format only verified order-scoped DraftOrderItems; no prices, internal IDs,
    wholesale data or external lookups. Bound long product names to Telegram's
    4096 UTF-16 message limit and preserve original quantities from the site.
    """
    if getattr(draft, "source", None) != "website":
        return []
    items = sorted(getattr(draft, "items", ()) or (), key=lambda item: item.id)
    if not items:
        return []
    rendered = []
    remaining = max_utf16
    for item in items:
        raw_title = " ".join((getattr(item, "raw_product_text", "") or "").split())
        if not raw_title:
            raw_title = "Товар без названия"
        title = raw_title[:120] + ("…" if len(raw_title) > 120 else "")
        line = f"• {title} – {item.qty} шт."
        length = len(line.encode("utf-16-le")) // 2 + 1
        if length > remaining:
            break
        rendered.append(line)
        remaining -= length
    if not rendered:
        return []
    missing = len(items) - len(rendered)
    if missing:
        rendered.append(f"… ещё {missing} позиций")
    return ["", "Ваши товары (по заказу на сайте):", *rendered]


def public_summary(desk, draft, order=None, *, include_items=True):
    label = stage_label(desk, draft, order)
    if order is None:
        lines = [f"Заявка №{desk.draft_id}: {label}"]
        if include_items:
            lines.extend(tilda_product_lines(draft))
        return "\n".join(lines)
    if getattr(order, "needs_review", False):
        label = "Менеджер уточняет условия заказа"
    label = {"В сборке": "Собираем ваш заказ", "Собран": "Готов к отправке", "Отгружен": "Передан в доставку"}.get(label, label)
    payment = PAYMENT_LABELS[order.payment_status]
    suffix = f". {payment}" if payment != label else ""
    from app.services.telegram_display import telegram_rubles
    from app.services.manager_workspace import DELIVERY_LABELS
    lines = [f"Заявка №{desk.draft_id}, заказ №{order.id}: {label}{suffix}."]
    if getattr(order, "total", None) is not None:
        lines.append(f"Сумма: {telegram_rubles(order.total)}")
    method = getattr(order, "delivery_method", "unselected")
    if method != "unselected":
        lines.append("Доставка: " + DELIVERY_LABELS[method])
    if getattr(order, "delivery_reference", None):
        lines.append("Номер доставки: " + order.delivery_reference)
    if include_items:
        lines.extend(tilda_product_lines(draft))
    return "\n".join(lines)


def order_details(order):
    return [f"Оплата: {PAYMENT_LABELS[order.payment_status]}",
            f"Сборка: {FULFILLMENT_LABELS[order.fulfillment_status]}",
            f"Доставка: {DELIVERY_STATUS[order.delivery_status]}"]
