from sqlalchemy import select
from sqlalchemy.orm import selectinload
from aiogram.types import InlineKeyboardMarkup
from app.database.session import async_session
from app.models.order_desk import OrderDesk
from app.models.draft_order import DraftOrder
from app.models.manager import Manager
from app.models.order import Order
from app.services.desk_status import stage_label, order_details
from app.services.order_desk import DeskError
from app.services.telegram_display import telegram_rubles
from app.services.tilda_availability import availability, stock_label


REVIEW_PROBLEM_LABELS = {
    "invalid_total": "Tilda не передала корректную итоговую сумму заказа",
    "total_mismatch": "сумма позиций, скидка и итог Tilda не совпадают",
    "invalid_discount": "скидка в заявке некорректна",
    "discount_requires_agreement": "скидка требует отдельного согласования",
    "item_total_mismatch": "итог одной из позиций не совпадает с ценой и количеством",
    "invalid_item_total": "некорректная сумма позиции",
    "invalid_reported_price": "в Tilda отсутствует или некорректна цена позиции",
    "invalid_quantity": "некорректное количество",
    "sku_missing": "артикул сайта не найден в МойСклад",
    "sku_ambiguous": "артикул сопоставляется с несколькими товарами",
    "sku_unmatched": "артикул сайта не сопоставлен с МойСклад",
    "catalog_unavailable": "не удалось обновить каталог МойСклад",
    "unsupported_currency": "валюта заказа не поддерживается",
}


def review_blockers(draft):
    details = draft.contact_details or {}
    problems = list(details.get("tilda_review_problems") or [])
    if draft.total is None and not problems:
        problems.append("invalid_total")
    if not draft.items:
        problems.append("empty_items")
    for item in draft.items:
        if not item.product_id or item.match_status != "matched":
            problems.append("sku_unmatched")
        if item.price is None or item.price <= 0 or item.qty <= 0:
            problems.append("invalid_reported_price")
    return list(dict.fromkeys(problems))


async def desk_card(draft_id, *, view="details"):
    # One complete, actionable card for initial notification AND every refresh.
    async with async_session() as session:
        desk = await session.get(OrderDesk, draft_id)
        draft = await session.scalar(select(DraftOrder).where(
            DraftOrder.id == draft_id).options(selectinload(DraftOrder.items)))
        if not desk or not draft:
            raise DeskError("Заявка не найдена.")
        data = draft.contact_details or {}
        stocks = await availability(draft.items)
        manager = await session.get(Manager, desk.manager_id) if desk.manager_id else None
        order = await session.get(Order, desk.order_id) if desk.order_id else None
        review_approved = bool(data.get("tilda_review_approved") and desk.order_id)
        active = draft.status != "rejected" and not desk.order_id
        problems = review_blockers(draft) if active else []
        stage = stage_label(desk, draft, order)
        lines = [
            f"Заявка с сайта №{draft.id}" + (f" · заказ №{desk.order_id}" if desk.order_id else ""),
            f"Статус: {stage}",
            f"Клиент: {draft.customer_name or 'не указан'}",
            f"Телефон: {data.get('phone') or 'не указан'}",
            f"Email: {draft.sender_email or 'не указан'}",
            f"Ответственный: {manager.name or manager.id if manager else 'не назначен'}",
            f"Telegram клиента: {'подключён' if desk.customer_telegram_id else 'не подключён'}",
            f"Оплата: {'оплачен' if order and order.payment_status == 'paid' else 'не оплачено'}",
        ]
        if order:
            lines.extend(order_details(order))
        if data.get("comment"):
            lines.append("Комментарий: " + str(data["comment"])[:450])
        if draft.total is not None:
            lines.append("Итог заказа: " + telegram_rubles(draft.total))
        else:
            lines.append("Итог заказа: требует проверки (не подтверждён)")
        if draft.items and all(i.price is not None for i in draft.items):
            lines.append("Сумма позиций по ценам сайта: " +
                telegram_rubles(sum(i.price * i.qty for i in draft.items)))
        reported = data.get("reported_total_minor")
        lines.append("Итог, переданный Tilda: " +
            (telegram_rubles(reported) if reported is not None else "отсутствует / некорректен"))
        lines.append("Цена сайта: " + ("подтверждена менеджером" if review_approved
                                     else "требует подтверждения менеджера"))
        lines.append("Товары:")
        # Telegram text is limited to 4096 UTF-16 units. Keep a safe margin.
        items = sorted(draft.items, key=lambda item: item.id)
        truncated_items = False
        for index, item in enumerate(items, 1):
            name = (item.raw_product_text or item.product_name or "Товар")[:85]
            price = telegram_rubles(item.price) if item.price is not None else "не указана"
            matched = (item.product_name or "не сопоставлен")[:75] if item.product_id else "не сопоставлен"
            line = (f"{index}. {name} × {item.qty} · {price} за шт.\n"
                    f"   МойСклад: {matched} · артикул {item.article or '—'}\n"
                    f"   {stock_label(item, stocks)}")
            if len(("\n".join(lines + [line])).encode("utf-16-le")) // 2 > 3250:
                lines.append(f"… Остальные позиции ({len(items) - index + 1} шт.) — по кнопке «Все товары»")
                truncated_items = True
                break
            lines.append(line)
        if active and problems:
            lines.append("⚠️ Нельзя подтвердить заказ:")
            for code in problems[:6]:
                lines.append("• " + REVIEW_PROBLEM_LABELS.get(code, code.replace("_", " ")))
            lines.append("Следующий шаг: проверить исходные данные Tilda и сопоставление товаров. "
                         "Недостающую сумму нельзя подтвердить автоматически.")
        elif active:
            lines.append("Следующий шаг: " + ("нажмите «Подтвердить заказ» и согласуйте сумму с клиентом"
                        if desk.manager_id else "нажмите «Взять заказ», затем «Подтвердить заказ»"))
        elif desk.order_id:
            lines.append("Следующий шаг: открыть заказ и выбрать склад/источники.")
        else:
            lines.append("Заявка отменена. Действий не требуется.")
        lines.append("Наличие показано справочно; резерв создаётся отдельно.")
        rows = []
        if active and not desk.manager_id:
            rows.append([{"text": "Взять заказ", "callback_data": f"desk:claim:{draft_id}"}])
        elif active and desk.manager_id:
            if problems:
                rows.append([{"text": "🔎 Перепроверить товары и сумму",
                              "callback_data": f"desk:recheck:{draft_id}"}])
            else:
                rows.append([{"text": "✅ Подтвердить заказ",
                              "callback_data": f"desk:confirm:{draft_id}:{draft.revision}"}])
            rows.append([{"text": "Отменить заявку", "callback_data": f"desk:cancel:{draft_id}"}])
        if desk.order_id:
            rows.append([{"text": "Открыть заказ",
                          "callback_data": f"order:refresh:{desk.order_id}:0"}])
            if order and order.payment_status == "unpaid" and order.fulfillment_status == "new" and order.status != "rejected":
                rows.append([{"text": "Уведомить: ожидает оплаты",
                              "callback_data": f"desk:payment:{draft_id}"}])
        if desk.customer_telegram_id:
            rows.append([{"text": "Ответить клиенту", "callback_data": f"desk:reply:{draft_id}"}])
        if truncated_items:
            rows.append([{"text": "Все товары", "callback_data": f"desk:items:{draft_id}"}])
        rows.append([{"text": "История действий", "callback_data": f"desk:history:{draft_id}"}])
        rows.append([{"text": "Обновить", "callback_data": f"desk:refresh:{draft_id}"}])
        text = "\n".join(lines)
        text = text.encode("utf-16-le")[:7800].decode("utf-16-le", errors="ignore")
        return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def desk_items(draft_id):
    async with async_session() as session:
        if not await session.get(OrderDesk, draft_id):
            raise DeskError("Заявка не найдена.")
        draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id).options(selectinload(DraftOrder.items)))
        stocks = await availability(draft.items)
        return [f"Заявка №{draft_id} · позиция {n}: {i.raw_product_text}\n"
            f"МойСклад: {i.product_name if i.product_id else i.match_status} ({i.product_id or '—'})\n"
            f"Артикул: {i.article or '—'} · цена Tilda (заявленная): "
            f"{i.qty} × {telegram_rubles(i.price) if i.price is not None else 'требует проверки'}\n"
            f"{stock_label(i, stocks)}."
            for n,i in enumerate(sorted(draft.items, key=lambda i: i.id),1)]


async def desk_history(draft_id):
    """Read-only audit, deliberately excluding free-text details and message bodies."""
    from app.models.order_desk import DeskEvent
    from app.models.operations import OrderEvent
    from datetime import timezone
    labels = {"tilda_order_created": "Заявка получена", "manager_claim_succeeded": "Назначен ответственный",
        "telegram_customer_linked": "Telegram клиента подключён", "tilda_review_approved": "Товары и сумма согласованы",
        "order_stage_changed": "Этап согласования изменён", "order_status_changed": "Статус заказа изменён",
        "customer_message_received": "Сообщение клиента принято", "manager_reply_queued": "Ответ поставлен в очередь",
        "tilda_order_cancelled": "Заявка отменена", "tilda_repriced": "Розничные цены обновлены",
        "delivery_reconciled": "Доставка сообщения проверена", "paid": "Оплата подтверждена",
        "assembling": "Сборка начата", "assembled": "Заказ собран", "shipped": "Отгрузка подтверждена",
        "delivery": "Условия/статус доставки изменены", "cancel": "Заказ отменён",
        "review": "Назначена проверка", "resolve": "Проверка завершена"}
    async with async_session() as session:
        desk = await session.get(OrderDesk, draft_id)
        if not desk:
            raise DeskError("Заявка не найдена.")
        records = []
        events = (await session.scalars(select(DeskEvent).where(DeskEvent.draft_id == draft_id)
            .order_by(DeskEvent.id.desc()).limit(20))).all()
        for event in events:
            actor = f"Telegram {event.actor_telegram_id}" if event.actor_telegram_id else "система"
            records.append((event.created_at, f"D{event.id}", labels.get(event.action, "Действие"), actor))
        if desk.order_id:
            events = (await session.scalars(select(OrderEvent).where(OrderEvent.order_id == desk.order_id)
                .order_by(OrderEvent.id.desc()).limit(20))).all()
            for event in events:
                records.append((event.created_at, f"O{event.id}", labels.get(event.action, "Действие"), f"менеджер #{event.manager_id}"))
        lines = [f"История заявки №{draft_id} · последние 20 действий · UTC"]
        for stamp, identifier, label, actor in sorted(records, reverse=True)[:20]:
            lines.append(f"{stamp.astimezone(timezone.utc):%d.%m %H:%M} · {identifier} · {label} · {actor}")
        return "\n".join(lines)
