"""Manager read models: cards never need provider requests."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database.session import async_session
from app.models.fulfillment import Shipment, WarehouseAllocation
from app.services.telegram_display import telegram_rubles, customer_type_label
from app.bot.manager_ui_flags import enabled as modern_ui_enabled

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


async def site_titles_for(order):
    """Optional, verified projection of original Tilda titles (read-only).

    A title is shown only if draft/order item ordering and immutable
    identities agree. Ambiguity falls back to the confirmed MoySklad name.
    """
    if not getattr(order, "desk_draft_id", None):
        return {}
    from app.models.draft_order import DraftOrderItem
    async with async_session() as session:
        originals = list((await session.scalars(
            select(DraftOrderItem).where(
                DraftOrderItem.draft_order_id == order.desk_draft_id)
            .order_by(DraftOrderItem.id))).all())
    confirmed = sorted(order.items, key=lambda row: row.id)
    if len(originals) != len(confirmed) or any(
        old.product_id != new.product_id or old.qty != new.qty or
        old.price != new.price
        for old, new in zip(originals, confirmed)):
        return {}
    return {new.id: old.raw_product_text for old, new in zip(originals, confirmed)
            if old.raw_product_text}


def order_details_card(order, shipments=()):
    from app.services.desk_status import stage_label
    lines = [f"Заказ №{order.id}", f"Статус: {stage_label(None, None, order)}", f"Клиент: {order.customer_name}", f"Тип: {customer_type_label(order.customer_type)}",
        f"Email: {getattr(order, 'customer_email', None) or 'не указан'}",
        f"Телефон: {order.phone or 'не указан'}", f"Telegram: {order.telegram or 'не указан'}",
        f"Источник: {SOURCE_LABELS.get(order.source, order.source)}",
        f"Сборка: {FULFILLMENT_LABELS[order.fulfillment_status]}", f"Оплата: {PAYMENT_LABELS[order.payment_status]}"]
    if order.source == "website":
        lines.append(f"Доставка: {DELIVERY_LABELS[order.delivery_method]} · {DELIVERY_STATUS[order.delivery_status]}")
        if order.delivery_reference:
            lines.append(f"Номер доставки: {order.delivery_reference}")
        if (order.fulfillment_status == "shipped"
                and order.delivery_status == "delivered" and not order.needs_review):
            lines.append("Заказ завершён: отгружен и доставлен.")
    elif order.fulfillment_status == "shipped" and not order.needs_review:
        lines.append("Складской цикл завершён: отгружен.")
    if not getattr(order, "manual_fulfillment", False):
        lines.append(f"Контрагент: {order.counterparty_name or order.counterparty_id or 'не выбран'}")
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


def is_closed_order(order):
    """Closed for display only; no mutation of fulfillment or payment states."""
    if order.fulfillment_status != "shipped":
        return False
    if order.source == "email":
        return order.payment_status == "paid"
    return order.source == "website" and order.delivery_status == "delivered"


def closed_order_card(order):
    """Short Telegram receipt. Full details remain accessible on request."""
    return "\n".join([
        f"✅ Заказ №{order.id} завершён",
        f"Клиент: {order.customer_name or 'не указан'}",
        f"Канал: {SOURCE_LABELS.get(order.source, order.source or 'не указан')}",
        f"Итого: {telegram_rubles(order.total)}",
        f"Статус: {'Отгружен и оплачен' if order.source == 'email' else 'Доставлен'}",
        "Информация и товары доступны по кнопке «Подробнее».",
    ])


def available_order_keyboard(order, shipments=()):
    buttons = []
    def add(label, action):
        buttons.append([{"text": label, "callback_data": f"order:{action}:{order.id}:{order.revision}"}])
    if order.fulfillment_status != "cancelled" and order.status != "rejected":
        # Retail/Tilda: manager can mark payment immediately.
        # Wholesale/Gmail: it remains a manual status action but is placed
        # after stock processing, because customers normally pay on pickup
        # or later. Neither channel gates warehouse actions on payment.
        email_order = getattr(order, "source", "website") == "email"
        if order.payment_status == "unpaid" and not email_order:
            add("Оплачен", "paid")
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
                can_start = getattr(order, "ui_supply_ready", can_start)
                if not waiting_supply and (not external_only or can_start) and (not manual or can_start):
                    add("Начать сборку" if can_start else "Распределить по складам", "assembling" if can_start else "allocate")
            elif order.fulfillment_status == "assembling":
                add("Заказ собран", "assembled")
            elif (order.fulfillment_status == "assembled"
                  and (getattr(order, "source", "website") != "website" or order.delivery_method != "unselected")
                  and getattr(order, "ui_supply_ready", True)):
                add("Отгружен", "shipped")
            # Internal review is an administrator-only state, not a customer-facing
            # or routine order-management button.
        if order.fulfillment_status != "shipped" and getattr(order, "ui_cancellable", True):
            add("Отменить заказ", "cancel_confirm")
        if getattr(order, "source", "website") == "website":
            if order.fulfillment_status != "shipped":
                for method in ("pickup", "manual", "cdek", "yandex"):
                    if method != order.delivery_method:
                        add(DELIVERY_LABELS[method], "delivery_" + method)
            elif order.delivery_status == "dispatched":
                add("Доставлен", "delivered")
    add("Обновить", "refresh")
    add("Все товары", "items")
    if getattr(order, "desk_draft_id", None):
        if getattr(order, "source", "website") == "website":
            buttons.append([{"text": "Заявка сайта / связь с клиентом",
                             "callback_data": f"desk:refresh:{order.desk_draft_id}"}])
    if getattr(order, "supply_has_external", False):
        add("Закупки по заказу", "procurement")
    # Last action for wholesale Gmail, even in expanded order actions.
    # Never requires payment before dispatch, and no MoySklad money writes.
    if (getattr(order, "source", "website") == "email"
            and order.payment_status == "unpaid"
            and order.fulfillment_status != "cancelled"
            and order.status != "rejected"):
        add("Оплачен", "paid")
    return {"inline_keyboard": buttons}


def order_keyboard(order, shipments=(), *, expanded=False):
    if modern_ui_enabled() and is_closed_order(order):
        rows = (
            [[{"text": "📦 Все товары", "callback_data": f"order:items:{order.id}:{order.revision}"}],
             [{"text": "← Свернуть", "callback_data": f"order:refresh:{order.id}:0"}]]
            if expanded else
            [[{"text": "📄 Подробнее", "callback_data": f"order:details:{order.id}:{order.revision}"}]]
        )
        rows.append([{"text": "🏠 Главное меню", "callback_data": "workspace:home"}])
        return {"inline_keyboard": rows}
    rows = available_order_keyboard(order, shipments)["inline_keyboard"]
    active = order.fulfillment_status not in {"cancelled", "shipped"} and order.status != "rejected"
    source = {"text": "Выбрать источник", "callback_data": f"order:sources:{order.id}:{order.revision}"}
    if active and getattr(order, "manual_fulfillment", False) and not order.needs_review:
        if order.fulfillment_status == "new":
            from app.services.order_source_service import OrderSourceService
            view = OrderSourceService.view(order, shipments)
            if view["items"] and all(i["assigned"] == i["qty"] for i in view["items"]):
                source["text"] = "Проверить поступление / резерв"
            rows.insert(0, [source])
        elif order.fulfillment_status in {"assembling", "assembled"}:
            source["text"] = "Складские документы"
            rows.insert(0, [source])
    if (active and getattr(order, "manual_fulfillment", False)
            and not getattr(order, "counterparty_id", None)):
        rows.insert(0, [{"text":"Сопоставить покупателя МойСклад",
                          "callback_data":f"order:buyer:{order.id}:{order.revision}"}])
    # Tilda payment stays first. Gmail payment belongs at the very end of
    # expanded actions and appears in the compact card only after shipment.
    email_order = getattr(order, "source", "website") == "email"
    paid_row = next((row for row in rows
                     if any(button["callback_data"].split(":")[1] == "paid"
                            for button in row)), None)
    if paid_row and not email_order:
        rows.remove(paid_row)
        rows.insert(0, paid_row)
    if expanded:
        if modern_ui_enabled():
            rows.append([{"text": "🏠 Главное меню", "callback_data": "workspace:home"}])
        return {"inline_keyboard": rows}
    # At the assembled Tilda boundary, verifying warehouse documents comes
    # before offering dispatch: an allocation is not a MoySklad reserve.
    from app.services.hatuna_post_pick import post_pick_enabled_for_order
    if (getattr(order, "manual_fulfillment", False)
            and order.fulfillment_status == "assembled"
            and post_pick_enabled_for_order(order.id)):
        priorities = ("buyer", "sources", "shipped", "delivered")
    else:
        priorities = ("buyer", "resolve", "assembling", "assembled", "shipped", "allocate", "sources", "procurement", "delivered")
    primary = next((b for action in priorities for row in rows for b in row
                    if b["callback_data"].split(":")[1] == action), None)
    if (active and not order.needs_review and getattr(order, "source", "website") == "website"
            and order.fulfillment_status == "assembled"
            and order.delivery_method == "unselected"):
        primary = {"text": "Выбрать доставку",
                   "callback_data": f"order:actions:{order.id}:{order.revision}"}
    result = [paid_row] if paid_row and not email_order else []
    if primary:
        result.append([primary])
    result.append([{"text": "⋯ Действия", "callback_data": f"order:actions:{order.id}:{order.revision}"}])
    if (email_order and paid_row and order.fulfillment_status == "shipped"):
        result.append(paid_row)
    if modern_ui_enabled():
        result.append([{"text": "🏠 Главное меню", "callback_data": "workspace:home"}])
    return {"inline_keyboard": result}



def order_card(order, shipments=(), *, site_titles=None):
    """Complete read-only manager card; buyer channel and stock source are distinct."""
    from app.services.desk_status import stage_label
    from app.services.order_source_service import allocation_ready
    source_labels = {"own": "Свой склад", "partner_x": "Склад Х",
                     "external": "Внешний поставщик"}
    item_sources = {item.id: [] for item in order.items}
    total_quantity = sum(item.qty for item in order.items)
    post_pick = False
    if getattr(order, "manual_fulfillment", False):
        from app.services.hatuna_post_pick import post_pick_enabled_for_order
        post_pick = post_pick_enabled_for_order(order.id)
    ready_quantity = 0
    assigned_quantity = 0
    for shipment in shipments:
        if shipment.status not in {"planned", "exported"}:
            continue
        for allocation in shipment.allocations:
            if allocation.supply_status == "cancelled":
                continue
            if getattr(allocation, "managed_source", False):
                assigned_quantity += allocation.qty
            ready = allocation_ready(allocation, manual=order.manual_fulfillment)
            if ready:
                ready_quantity += allocation.qty
            label = source_labels.get(allocation.source_type, "Неизвестный источник")
            if (post_pick and allocation.source_type == "partner_x"
                    and allocation.supply_status in {"awaiting_receipt", "received"}):
                state = ("назначено, сборщик проверяет наличие"
                         if order.fulfillment_status in {"new", "assembling"}
                         else "ожидает внутреннего оприходования")
            else:
                state = ("готово" if ready else
                         "ожидает поступления" if allocation.source_type != "own"
                         else "ожидает проверки")
            item_sources.setdefault(allocation.order_item_id, []).append(
                (label, allocation.qty, state))
    all_sources = list(dict.fromkeys(
        source for entries in item_sources.values() for source, _, _ in entries))
    lines = [
        f"Заказ №{order.id}",
        f"Статус: {stage_label(None, None, order)}",
        f"Клиент: {order.customer_name or 'не указан'}",
        f"Телефон: {order.phone or 'не указан'}",
        f"Канал: {SOURCE_LABELS.get(order.source, order.source or 'не указан')}",
        "Источник товара: " + (", ".join(all_sources) if all_sources else "не назначен"),
        "Товары:",
    ]
    for index, item in enumerate(order.items, 1):
        confirmed_name = (item.name or "Без названия").strip()
        site_name = ((site_titles or {}).get(item.id) or confirmed_name).strip()
        line = f"{index}. {site_name[:150]} · {item.qty} шт. × {telegram_rubles(item.price)}"
        if site_name.casefold() != confirmed_name.casefold():
            line += "\n   МойСклад: " + confirmed_name[:125]
        assigned = item_sources.get(item.id) or []
        if assigned:
            line += "\n   " + "; ".join(f"{source}: {count} шт. ({state})"
                                       for source, count, state in assigned)
        if len("\n".join(lines + [line]).encode("utf-16-le")) // 2 > 2850:
            lines.append(f"… Ещё позиций: {len(order.items) - index + 1}. См. «Все товары».")
            break
        lines.append(line)
    lines.extend([
        f"Итого: {telegram_rubles(order.total)}",
        f"Оплата: {PAYMENT_LABELS.get(order.payment_status, order.payment_status)}",
        f"Сборка: {FULFILLMENT_LABELS.get(order.fulfillment_status, order.fulfillment_status)}",
    ])
    if post_pick:
        lines.append(f"Распределено по складам: {min(assigned_quantity, total_quantity)} из {total_quantity} шт.")
        lines.append("Физическое наличие подтверждает сборщик; распределение не равно оприходованию.")
    else:
        lines.append(f"Готово к сборке: {min(ready_quantity, total_quantity)} из {total_quantity} шт.")
    pending_x = any(shipment.status in {"planned", "exported"} and
        allocation.source_type == "partner_x" and
        allocation.supply_status in {"awaiting_receipt", "received"}
        for shipment in shipments for allocation in shipment.allocations)
    if order.needs_review:
        next_step = "устранить замечания к заказу."
    elif post_pick and order.fulfillment_status == "new":
        next_step = "сверить физическое наличие всех назначенных товаров → начать сборку."
    elif post_pick and order.fulfillment_status == "assembling":
        next_step = "проверить фактическое наличие и комплектность двух складских позиций → подтвердить «Заказ собран»."
    elif post_pick and order.fulfillment_status == "assembled":
        if pending_x:
            from app.config.settings import settings
            if settings.external_writes_enabled:
                next_step = "после проверки учёта провести приход Х → резерв по складам → отгрузку."
            else:
                next_step = ("выполнить проверку post-pick; реальные приход Х, резерв "
                             "и отгрузка отключены до отдельной приёмки.")
        else:
            next_step = "сверить созданные документы МойСклад и выполнить дальнейшую отгрузку."
    elif post_pick and order.fulfillment_status == "shipped":
        next_step = ("сверить документы отгрузки и доставку." if order.source == "website"
                     else "складские документы оформлены; заказ отгружен.")
    elif pending_x:
        next_step = "склад Х → подтвердить физическое получение → оприходовать в МойСклад → резерв → сборка → отгрузка."
    elif ready_quantity < total_quantity:
        next_step = "назначить и подтвердить источники товаров."
    elif order.fulfillment_status == "new":
        next_step = "создать подтверждённый резерв МойСклад и начать сборку."
    elif order.fulfillment_status == "assembled":
        next_step = "оформить отгрузку МойСклад."
    else:
        next_step = "открыть действия заказа."
    lines.append("Следующий шаг: " + next_step)
    full = "\n".join(lines)
    raw = full.encode("utf-16-le")
    return full if len(raw) <= 7700 else raw[:7600].decode("utf-16-le", errors="ignore")
