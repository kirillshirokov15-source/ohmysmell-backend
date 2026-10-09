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


async def desk_card(draft_id):
    async with async_session() as session:
        desk = await session.get(OrderDesk, draft_id)
        draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id).options(selectinload(DraftOrder.items)))
        if not desk or not draft:
            raise DeskError("Заявка не найдена.")
        manager = await session.get(Manager, desk.manager_id) if desk.manager_id else None
        order = await session.get(Order, desk.order_id) if desk.order_id else None
        stage = stage_label(desk, draft, order)
        lines = [f"Заказ с сайта · заявка №{draft.id}", f"Клиент: {draft.customer_name}",
            f"Телефон: {draft.contact_details.get('phone') or 'не указан'}", f"Email: {draft.sender_email or 'не указан'}",
            f"Ответственный: {manager.name or manager.id if manager else 'не назначен'}",
            f"Этап: {stage}", f"Telegram клиента: {'подключён' if desk.customer_telegram_id else 'не подключён'}"]
        if desk.stage == "new" and not desk.order_id and draft.status != "rejected":
            lines[0] = "Новый заказ с сайта · заявка №" + str(draft.id)
        if desk.order_id:
            lines.append(f"Заказ №{desk.order_id}")
        if order:
            lines.extend(order_details(order))
        if draft.contact_details.get("comment"):
            lines.append("Комментарий клиента: " + draft.contact_details["comment"][:500])
        lines.append("Сумма: " + (telegram_rubles(draft.total) if draft.total is not None else "требует проверки"))
        if all(i.price is not None for i in draft.items):
            lines.append("Розничная сумма для согласования: " + telegram_rubles(sum(i.price * i.qty for i in draft.items)))
        reported = draft.contact_details.get("reported_total_minor")
        lines.append("Заявлено сайтом: " + (telegram_rubles(reported) if reported is not None else "некорректно/не указано"))
        if not desk.order_id:
            lines.append("Проверка: " + (draft.review_notes or "Требуется согласование"))
        lines.append("Источники и наличие проверить. Сборка и отгрузка вручную.")
        lines.append(f"Все позиции с ценами: /siteitems {draft_id}")
        lines += [f"{index}. {i.raw_product_text[:80]} × {i.qty}" for index, i in enumerate(draft.items[:15], 1)]
        if len(draft.items) > 15:
            lines.append(f"Всего позиций: {len(draft.items)}. Остальные: /siteitems {draft_id}")
        rows = [[{"text": "Взять заказ", "callback_data": f"desk:claim:{draft_id}"}]] if not desk.manager_id and draft.status != "rejected" and not desk.order_id else []
        if desk.customer_telegram_id:
            rows.append([{"text": "Ответить клиенту", "callback_data": f"desk:reply:{draft_id}"}])
        if not desk.order_id and draft.status != "rejected":
            rows += [[{"text": "Ожидает подтверждения", "callback_data": f"desk:wait:{draft_id}"}],
                [{"text": "Подтвердить товары и розничную сумму", "callback_data": f"desk:confirm:{draft_id}:{draft.revision}"}]]
            rows.append([{"text": "Отменить заявку", "callback_data": f"desk:cancel:{draft_id}"}])
        if desk.order_id:
            rows.append([{"text": "Открыть заказ", "callback_data": f"order:refresh:{desk.order_id}:0"}])
            if order and order.payment_status == "unpaid" and order.fulfillment_status == "new" and order.status != "rejected":
                rows.append([{"text": "Уведомить: ожидает оплаты", "callback_data": f"desk:payment:{draft_id}"}])
        rows.append([{"text": "История действий", "callback_data": f"desk:history:{draft_id}"}])
        rows.append([{"text": "Обновить", "callback_data": f"desk:refresh:{draft_id}"}])
        # Bound in UTF-16 units, including astral emoji.
        text = "\n".join(lines).encode("utf-16-le")[:7800].decode("utf-16-le", errors="ignore")
        return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def desk_items(draft_id):
    async with async_session() as session:
        if not await session.get(OrderDesk, draft_id):
            raise DeskError("Заявка не найдена.")
        draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id).options(selectinload(DraftOrder.items)))
        return [f"Заявка №{draft_id} · позиция {n}: {i.raw_product_text}\nТовар: {i.product_id or 'сопоставление не задано'}\n"
            f"{i.qty} × {telegram_rubles(i.price) if i.price is not None else 'розничная цена требует проверки'}"
            for n,i in enumerate(draft.items,1)]


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
