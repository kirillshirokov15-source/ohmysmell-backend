import os

from sqlalchemy import select

from app.database.session import async_session
from app.integrations.http_tls import verified_session
from app.models.draft_order import DraftOrder
from app.models.manager import Manager
from app.services.telegram_display import (
    customer_type_label,
    draft_status_label,
    product_match_label,
    review_problem_lines,
    telegram_rubles,
)


def build_draft_card(draft: DraftOrder) -> str:
    lines = [
        "📨 Новый заказ из почты",
        "",
        f"Черновик №{draft.id}",
        "",
        f"Клиент: {draft.customer_name or 'Не указан'}",
        f"Email: {draft.sender_email}",
        f"Тип клиента: {customer_type_label(draft.customer_type)}",
        f"Тема: {draft.subject or 'Без темы'}",
        f"Контрагент: {draft.counterparty_name or draft.counterparty_id or 'Не выбран'}",
        "",
        "Позиции:",
    ]
    for item in draft.items:
        lines.extend(["", f"• {item.raw_product_text}"])
        if item.price is not None and item.item_total is not None:
            lines.append(
                f"  {item.qty} шт. × {telegram_rubles(item.price)} "
                f"= {telegram_rubles(item.item_total)}"
            )
        else:
            lines.append(
                f"  {item.qty} шт. — {product_match_label(item.match_status)}"
            )

    lines.extend([
        "",
        "Итого: "
        + (
            telegram_rubles(draft.total)
            if draft.total is not None
            else "Не рассчитано"
        ),
        f"Статус: {draft_status_label(draft.status)}",
    ])
    problems = review_problem_lines(draft)
    if draft.counterparty_candidates:
        lines.extend(["", "Возможные контрагенты:"])
        lines.extend(
            f"• {candidate.get('name') or 'Без имени'}"
            for candidate in draft.counterparty_candidates[:5]
        )
    if problems:
        lines.extend(["", "⚠️ Требует проверки:"])
        lines.extend(f"• {problem}" for problem in problems)
    return "\n".join(lines)


def build_draft_keyboard(draft: DraftOrder) -> dict:
    if str(draft.status) in {"new", "rejected"}:
        return {"inline_keyboard": []}
    rows = [[
        {
            "text": "Подтвердить: опт",
            "callback_data": f"draft:type:wholesale:{draft.id}",
        },
        {
            "text": "Подтвердить: розница",
            "callback_data": f"draft:type:retail:{draft.id}",
        },
    ]]
    for item in draft.items:
        for candidate in item.candidates[:3]:
            selected = item.product_id == candidate.get("id")
            reference = candidate.get("article") or (
                f"оценка {candidate.get('score')}"
                if candidate.get("score") is not None
                else "Товар"
            )
            marker = "✓ " if selected else ""
            rows.append([{
                "text": (
                    f"{marker}{reference} · "
                    f"{candidate.get('name', 'Товар')[:28]}"
                ),
                "callback_data": (
                    f"draft:product:{draft.id}:{item.id}:{candidate['id']}"
                ),
            }])
    for candidate in draft.counterparty_candidates[:3]:
        rows.append([{
            "text": (
                "Контрагент: "
                f"{(candidate.get('name') or 'Без имени')[:25]}"
            ),
            "callback_data": f"draft:counterparty:{draft.id}:{candidate['id']}",
        }])
    if not draft.counterparty_id and not draft.counterparty_candidates:
        rows.append([{
            "text": "Выбрать контрагента",
            "callback_data": f"draft:counterparty_select:{draft.id}",
        }])

    statuses = {str(item.match_status) for item in draft.items}
    if "ambiguous" in statuses or "not_found" in statuses:
        rows.append([{
            "text": (
                "Сопоставить товары"
                if "not_found" in statuses
                else "Выбрать товар"
            ),
            "callback_data": f"draft:ambiguous:{draft.id}",
        }])

    final_actions = []
    if str(draft.status) == "ready":
        final_actions.append({
            "text": "Подтвердить заказ",
            "callback_data": f"draft:finalize:{draft.id}",
        })
    final_actions.append({
        "text": "Отклонить",
        "callback_data": f"draft:reject:{draft.id}",
    })
    rows.append(final_actions)
    return {"inline_keyboard": rows}


async def notify_managers_about_draft(draft: DraftOrder) -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return 0
    async with async_session() as database_session:
        result = await database_session.execute(
            select(Manager).where(Manager.is_active.is_(True))
        )
        managers = result.scalars().all()

    http_session = verified_session()
    keyboard = build_draft_keyboard(draft)
    sent_count = 0
    for manager in managers:
        response = http_session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": manager.telegram_id,
                "text": build_draft_card(draft),
                "reply_markup": keyboard,
            },
            timeout=20,
        )
        response.raise_for_status()
        sent_count += 1
    return sent_count
