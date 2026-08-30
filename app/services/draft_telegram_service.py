import os

import requests
from sqlalchemy import select

from app.database.session import async_session
from app.models.draft_order import DraftOrder
from app.models.manager import Manager
from app.services.money import format_rubles


def build_draft_card(draft: DraftOrder) -> str:
    lines = [
        "📧 Новый заказ из Email",
        "",
        f"Draft: #{draft.id}",
        f"Клиент: {draft.customer_name or '—'}",
        f"Email: {draft.sender_email}",
        f"Customer type: {draft.customer_type}",
        f"Тема: {draft.subject or '—'}",
        f"Контрагент: {draft.counterparty_name or draft.counterparty_id or 'не выбран'}",
        "",
        "Позиции:",
    ]
    for item in draft.items:
        price = (
            f", {format_rubles(item.price)} ₽" if item.price is not None else ""
        )
        lines.append(
            f"• {item.raw_product_text} × {item.qty} — {item.match_status}{price}"
        )
    lines.extend(
        [
            "",
            "Итого: "
            + (
                f"{format_rubles(draft.total)} ₽"
                if draft.total is not None
                else "не рассчитано"
            ),
            f"Статус: {draft.status}",
        ]
    )
    if draft.review_notes:
        lines.extend(["", "Требует проверки:", draft.review_notes])
    if draft.counterparty_candidates:
        lines.extend(["", "Кандидаты контрагента:"])
        lines.extend(
            f"• {candidate.get('name') or 'Без имени'} "
            f"({candidate.get('email') or candidate.get('phone') or candidate['id']})"
            for candidate in draft.counterparty_candidates
        )
    return "\n".join(lines)


def build_draft_keyboard(draft: DraftOrder) -> dict:
    if str(draft.status) in {"new", "rejected"}:
        return {"inline_keyboard": []}
    rows = [
        [
            {
                "text": "Confirm wholesale",
                "callback_data": f"draft:type:wholesale:{draft.id}",
            },
            {
                "text": "Confirm retail",
                "callback_data": f"draft:type:retail:{draft.id}",
            },
        ]
    ]
    for item in draft.items:
        for candidate in item.candidates[:3]:
            rows.append(
                [{
                    "text": f"✓ {candidate.get('name', 'Product')[:35]}",
                    "callback_data": (
                        f"draft:product:{draft.id}:{item.id}:{candidate['id']}"
                    ),
                }]
            )
    for candidate in draft.counterparty_candidates[:3]:
        rows.append(
            [{
                "text": f"Контрагент: {(candidate.get('name') or 'Без имени')[:25]}",
                "callback_data": f"draft:counterparty:{draft.id}:{candidate['id']}",
            }]
        )
    rows.extend(
        [
            [{
                "text": "View ambiguous items",
                "callback_data": f"draft:ambiguous:{draft.id}",
            }],
            [
                {
                    "text": "Finalize",
                    "callback_data": f"draft:finalize:{draft.id}",
                },
                {
                    "text": "Reject",
                    "callback_data": f"draft:reject:{draft.id}",
                },
            ],
        ]
    )
    return {"inline_keyboard": rows}


async def notify_managers_about_draft(draft: DraftOrder) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    async with async_session() as session:
        result = await session.execute(
            select(Manager).where(Manager.is_active.is_(True))
        )
        managers = result.scalars().all()

    keyboard = build_draft_keyboard(draft)
    for manager in managers:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": manager.telegram_id,
                "text": build_draft_card(draft),
                "reply_markup": keyboard,
            },
            timeout=20,
        )
        response.raise_for_status()
