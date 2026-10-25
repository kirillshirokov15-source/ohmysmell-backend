import os
import asyncio

from sqlalchemy import select

from app.database.session import async_session
from app.integrations.http_tls import verified_session
from app.models.draft_order import DraftOrder
from app.models.manager import Manager
from app.models.sales import channel_customer_type
from app.services.email_order_quote import compact_quote, full_quote
from app.services.telegram_display import (
    customer_type_label,
    draft_status_label,
    product_match_label,
    review_problem_lines,
    telegram_rubles,
)


def inquiry_original_body(draft) -> str:
    """Read the full original email from its persisted inbound message."""
    inbound = getattr(draft, "inbound_message", None)
    raw = (getattr(inbound, "body_text", None)
           or getattr(draft, "body_text", None) or "")
    return raw.replace("\x00", "").strip()


def old_inquiry_order_lines(draft):
    """Show previously misclassified email lines without mutating the saved draft."""
    if ((getattr(draft, "contact_details", None) or {}).get("mail_intent") != "inquiry"
            or getattr(draft, "source", None) != "email"):
        return ()
    from app.services.email_parser import EmailParser
    classified = EmailParser().parse_message(
        getattr(draft, "subject", None),
        inquiry_original_body(draft),
        sender_name=getattr(draft, "customer_name", None))
    return classified.items if classified.intent == "order" and classified.items else ()


def build_inquiry_message(draft) -> str:
    """Dedicated manager Telegram message, independent of editMessageText."""
    body = inquiry_original_body(draft)
    subject = (getattr(draft, "subject", None) or "Без темы").strip()[:250]
    sender = (getattr(draft, "sender_email", None) or "Не указан").strip()[:320]
    if not body:
        body = "Текст письма не найден в CRM. Проверьте исходное письмо в Gmail."
    return (f"📩 Вопрос клиента · обращение №{draft.id}\n"
            f"От: {sender}\n"
            f"Тема: {subject}\n\n"
            f"{body[:3000]}\n\n"
            "Не заказ. Ответьте клиенту в переписке Gmail.")[:3850]


def build_draft_card(draft: DraftOrder) -> str:
    mail_intent = (getattr(draft, "contact_details", None) or {}).get("mail_intent")
    inquiry = mail_intent == "inquiry"
    tentative = mail_intent == "possible_order"
    email_heading = ("📩 Письмо-уточнение · НЕ заказ" if inquiry
                     else "⚠️ Возможный заказ · товар в теме" if tentative
                     else "📨 Новый заказ из почты" if draft.items
                     else "📨 Возможный заказ · нужны товары")
    lines = [
        email_heading if getattr(draft, "source", "email") == "email" else "📨 Входящая заявка",
        "",
        f"Черновик №{draft.id}",
        "",
        f"Клиент: {draft.customer_name or 'Не указан'}",
        f"Email: {draft.sender_email}",
        f"Тип клиента: {customer_type_label(draft.customer_type)}",
        "Источник: " + {"email": "Email", "website": "Сайт", "telegram": "Telegram",
            "instagram": "Instagram", "manual": "Менеджер"}.get(getattr(draft, "source", "email"), "Не указан"),
        f"Тема: {draft.subject or 'Без темы'}",
        f"Контрагент: {draft.counterparty_name or draft.counterparty_id or 'Не выбран'}",
        "",
        "Позиции:" if not inquiry else "Письмо клиента (требуется ответ):",
    ]
    if inquiry:
        candidate_lines = old_inquiry_order_lines(draft)
        if candidate_lines:
            lines.append("⚠️ Исходное письмо содержит товарные строки заказа. "
                         "Используйте «Разобрать как заказ» – существующая заявка не дублируется.")
            for line in candidate_lines[:5]:
                lines.append(f"• {line.raw_product_text[:100]} · "
                             + (f"{line.qty} шт." if line.qty > 0 else "количество уточнить"))
        excerpt = " ".join(inquiry_original_body(draft).split())[:650]
        lines.append(excerpt or "(письмо без текста; посмотрите исходное письмо в Gmail)")
        lines.append("⚠ Это письмо не является оформленным заказом.")
    if tentative:
        lines.append("⚠ Тема похожа на товар. Подтвердите, что это заказ, и уточните количество.")
    for item in draft.items:
        reference = f" (позиция {item.id})" if getattr(item, "id", None) else ""
        lines.extend(["", f"• {item.raw_product_text}{reference}"])
        confidence = getattr(item, "quantity_confidence", "confirmed")
        if confidence != "confirmed":
            lines.append(f"⚠ Количество: {item.qty if item.qty > 0 else 'неизвестно'} · {confidence}. Подтвердите или исправьте.")
        if item.price is not None and item.item_total is not None:
            lines.append(
                f"  {item.qty} шт. × {telegram_rubles(item.price)} "
                f"= {telegram_rubles(item.item_total)}"
            )
        else:
            lines.append(
                f"  {item.qty} шт. — {product_match_label(item.match_status)}"
            )

    if getattr(draft, "source", None) == "email" and not inquiry:
        quote = full_quote(draft)
        if quote:
            lines.extend(["", "Цены/валюта, заявленные в исходном письме:"])
            lines.extend(quote)
            lines.append("Это данные клиента, не цены МойСклад и не итог CRM.")

    contact = getattr(draft, "contact_details", None) or {}
    policy = contact.get("customer_type_policy", {})
    if policy.get("profile_conflict"):
        lines.append(f"Профиль клиента: {customer_type_label(policy['profile_type'])}. "
                     "Условия заказа определены каналом; профиль не изменён.")
    if contact.get("telegram"):
        lines.append(f"Telegram: {contact['telegram']}")
    if contact.get("phone"):
        lines.append(f"Телефон: {contact['phone']}")
    if contact.get("comment"):
        lines.append(f"Комментарий: {contact['comment'][:500]}")

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
        lines.extend(["", "Возможные контрагенты (проверьте перед привязкой):"])
        lines.extend(
            f"• {candidate.get('name') or 'Без имени'} · "
            f"{candidate.get('reason') or 'ручная проверка'}"
            for candidate in draft.counterparty_candidates[:5]
        )
    if getattr(draft, "source", None) == "email" and not draft.counterparty_id:
        lines.extend(["", "Контрагент: выберите вариант МойСклад или воспользуйтесь кнопкой ручного поиска."])
    audit = (getattr(draft, "contact_details", None) or {}).get("counterparty_reconciliation")
    if audit:
        lines.append("Сверка контрагента: " + str(audit.get("evidence") or "подтверждено")[:100])
    if problems:
        lines.extend(["", "⚠️ Требует проверки:"])
        lines.extend(f"• {problem}" for problem in problems)
    return "\n".join(lines)[:4000]


def build_draft_keyboard(draft: DraftOrder) -> dict:
    if (getattr(draft, "contact_details", None) or {}).get("tilda"):
        return {"inline_keyboard": [[{"text": "Открыть заявку сайта", "callback_data": f"desk:refresh:{draft.id}"}]]}
    if str(draft.status) in {"new", "rejected"}:
        return {"inline_keyboard": []}
    if (getattr(draft, "contact_details", None) or {}).get("mail_intent") == "inquiry":
        rows = [
            [{"text": "📄 Прочитать вопрос", "callback_data": f"draft:details:{draft.id}"}],
        ]
        # Also works with previously ingested inquiries: an apparent product
        # in the original subject can be reviewed without a second email.
        from app.services.email_parser import EmailParser
        classification = EmailParser().parse_message(
            getattr(draft, "subject", None),
            inquiry_original_body(draft),
            sender_name=getattr(draft, "customer_name", None),
        )
        if classification.intent in {"possible_order", "order"} and classification.items:
            rows.append([{"text": ("📦 Проверить как возможный заказ"
                                      if classification.intent == "possible_order"
                                      else "🧾 Разобрать как заказ из письма"),
                          "callback_data": f"draft:reconsider:{draft.id}"}])
        rows.extend([
            [{"text": "Закрыть обращение", "callback_data": f"draft:reject:{draft.id}"}],
            [{"text": "Обновить", "callback_data": f"draft:refresh:{draft.id}"}],
        ])
        if isinstance(getattr(draft, "revision", None), int):
            for row in rows:
                for button in row:
                    button["callback_data"] += f":v{draft.revision}"
        return {"inline_keyboard": rows}
    rows = []
    if (getattr(draft, "contact_details", None) or {}).get("mail_intent") == "possible_order":
        rows.append([
            {"text": "✅ Это заказ", "callback_data": f"draft:confirmorder:{draft.id}"},
            {"text": "💬 Это вопрос", "callback_data": f"draft:classifyinquiry:{draft.id}"},
        ])
    rows += [] if channel_customer_type(getattr(draft, "source", "email")) else [[
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
        if getattr(item, "quantity_confidence", "confirmed") != "confirmed":
            rows.append([{"text": f"#{item.id}: {q}", "callback_data": f"draft:qty:{draft.id}:{item.id}:{q}"} for q in range(1,6)])
            rows.append([{"text": f"#{item.id}: ввести вручную", "callback_data": f"draft:qtymanual:{draft.id}:{item.id}"}])
        elif hasattr(item, "qty"):
            rows.append([{"text": f"#{item.id}: изменить количество ({item.qty})", "callback_data": f"draft:qtymanual:{draft.id}:{item.id}"}])
        for candidate_index, candidate in enumerate(item.candidates[:3]):
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
                    f"draft:pick:{draft.id}:{item.id}:{candidate_index}"
                ),
            }])
    for item in draft.items:
        if str(item.match_status) in {"not_found", "ambiguous"}:
            rows.append([{"text": f"🔎 #{item.id}: другой товар / нет в МойСклад",
                          "callback_data": f"draft:searchhint:{draft.id}:{item.id}"}])
    for candidate_index, candidate in enumerate(draft.counterparty_candidates[:5]):
        rows.append([{
            "text": (
                "Контрагент: "
                f"{(candidate.get('name') or 'Без имени')[:22]} "
                + ("✓ email" if str(candidate.get("reason") or "").startswith("Email")
                   else "⚠ сверить")
            ),
            "callback_data": f"draft:cp:{draft.id}:{candidate_index}",
        }])
    if not draft.counterparty_id:
        rows.append([{
            "text": "🔎 Сверить контрагентов МойСклад",
            "callback_data": f"draft:counterparty_select:{draft.id}",
        }])
        if getattr(draft, "source", None) == "email":
            rows.append([{"text": "✍️ Ввести контрагента вручную",
                          "callback_data": f"draft:cpmanual:{draft.id}"}])
            rows.append([{"text": "➕ Создать нового контрагента",
                          "callback_data": f"draft:cpnew:{draft.id}"}])

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
    if str(draft.status) == "ready" and (
            (getattr(draft, "contact_details", None) or {}).get("mail_intent")
            not in {"possible_order", "inquiry"}):
        final_actions.append({
            "text": "Подтвердить заказ",
            "callback_data": f"draft:finalize:{draft.id}",
        })
    final_actions.append({
        "text": "Отклонить",
        "callback_data": f"draft:reject:{draft.id}",
    })
    rows.append(final_actions)
    if hasattr(draft, "revision") and isinstance(draft.revision, int):
        for row in rows:
            for button in row:
                button["callback_data"] += f":v{draft.revision}"
        # Telegram callback data has a 64-byte limit.
        rows = [row for row in rows if all(len(b["callback_data"].encode()) <= 64 for b in row)]
    return {"inline_keyboard": rows}


def counterparty_review_card(draft, *, manual_search=False):
    """Focused Gmail/MoySklad identity screen; no unrelated order actions.

    A suggested record has NOT yet been matched or linked. Final provider
    identity and CRM uniqueness checks remain in link_counterparty().
    """
    candidates = list(getattr(draft, "counterparty_candidates", None) or [])[:5]
    lines = [f"🔎 Сверка контрагента МойСклад · заявка №{draft.id}"]
    if getattr(draft, "counterparty_id", None):
        lines.append("✅ Контрагент уже выбран. Вернитесь к заявке.")
    elif candidates:
        lines.append(f"Найдено вариантов: {len(candidates)}. Выберите правильную карточку.")
        lines.append("Кандидаты ещё не подтверждены: проверьте юридическое лицо, email и ИНН.")
        for index, candidate in enumerate(candidates, 1):
            name = " ".join(str(candidate.get("name") or "Без имени").split())[:110]
            reason = " ".join(str(candidate.get("reason") or "Требует сверки").split())[:120]
            lines.append(f"{index}. {name} · {reason}")
    else:
        lines.append("Точных совпадений не найдено."
                     if not manual_search else "По ручному запросу контрагенты не найдены.")
        lines.append("Нажмите «Ввести контрагента вручную» для поиска по названию, email или ИНН."
                     " Если карточки действительно нет, нажмите «Создать нового контрагента»."
                     " Создание новой карточки потребует отдельной проверки дублей и подтверждения.")
    return "\n".join(lines)[:3800]


def counterparty_review_keyboard(draft):
    """Only matched alternatives and actionable manual/create controls.

    Reuse full-keyboard callbacks so revision tags and existing identity
    verification remain unchanged. The compact draft keyboard intentionally
    hides those controls and must never be used for this screen.
    """
    if (getattr(draft, "source", None) != "email"
            or getattr(draft, "counterparty_id", None)):
        return {"inline_keyboard": [[
            {"text": "← К заявке", "callback_data": f"draft:refresh:{draft.id}"}
        ]]}
    full = build_draft_keyboard(draft)["inline_keyboard"]
    allowed = (":cp:", ":cpmanual:", ":cpnew:")
    rows = [row for row in full
            if any(tag in btn.get("callback_data", "") for btn in row for tag in allowed)]
    if not getattr(draft, "counterparty_id", None):
        revision = getattr(draft, "revision", None)
        suffix = f":v{revision}" if isinstance(revision, int) else ""
        rows.append([{"text": "🔄 Повторить сверку МойСклад",
                      "callback_data": f"draft:counterparty_select:{draft.id}{suffix}"}])
    rows.append([{"text": "← К заявке",
                  "callback_data": f"draft:refresh:{draft.id}"}])
    return {"inline_keyboard": rows}


def compact_draft_keyboard(draft, *, expanded=False):
    full = build_draft_keyboard(draft)
    if expanded:
        return full
    buttons = [button for row in full["inline_keyboard"] for button in row]
    primary = next((b for b in buttons if ":finalize:" in b["callback_data"]), None)
    if (getattr(draft, "contact_details", None) or {}).get("tilda"):
        primary = buttons[0] if buttons else None
    elif not primary and str(draft.status) not in {"new", "rejected"}:
        if (getattr(draft, "contact_details", None) or {}).get("mail_intent") == "inquiry":
            primary = next((b for b in buttons if ":reconsider:" in b["callback_data"]), None)
        wording = "🧾 Проверить заказ" if (
            getattr(draft, "source", None) == "email"
            and (getattr(draft, "contact_details", None) or {}).get("mail_intent") != "inquiry"
        ) else "Проверить заявку"
        if primary is None:
            primary = {"text": wording, "callback_data": f"draft:actions:{draft.id}"}
    rows = [[primary]] if primary else []
    rows += [[{"text": label, "callback_data": f"draft:{action}:{draft.id}"}]
             for label, action in (("Подробнее", "details"), ("⋯ Действия", "actions"))]
    return {"inline_keyboard": rows}


def compact_draft_card(draft, *, expanded=False):
    if expanded:
        return build_draft_card(draft)
    rows = compact_draft_keyboard(draft)["inline_keyboard"]
    next_step = rows[0][0]["text"] if len(rows) > 2 else "Действий не требуется"
    email = getattr(draft, "source", "email") == "email"
    mail_intent = (getattr(draft, "contact_details", None) or {}).get("mail_intent")
    inquiry = mail_intent == "inquiry"
    historic_order_lines = old_inquiry_order_lines(draft) if inquiry else ()
    tentative = mail_intent == "possible_order"
    visible_status = draft_status_label(draft.status)
    if email and not inquiry and str(draft.status) == "needs_review":
        visible_status = "Заказ получен · требуется проверить товары/контрагента"
    lines = (["⚠️ Сохранённое письмо содержит заказ · требуется разбор"] if historic_order_lines else
             ["📩 Уточнение по почте · не заказ"] if inquiry else
             ["⚠️ Возможный заказ · товар в теме"] if tentative else
             ["📦 Оптовый заказ из почты"] if email else []) + [
        f"Заявка №{draft.id}", f"Статус: {visible_status}"]
    if email:
        lines.append("Клиент: " + (draft.customer_name or draft.sender_email or "Не указан")[:120])
        if inquiry:
            lines.append("Тема: " + (draft.subject or "Без темы")[:170])
            if historic_order_lines:
                lines.append("Товарные строки в оригинале:")
                for item in historic_order_lines[:4]:
                    lines.append(f"• {item.raw_product_text[:100]} · "
                                 + (f"{item.qty} шт." if item.qty > 0 else "количество уточнить"))
                lines.append("Сейчас: «Разобрать как заказ» – без повторного чтения Gmail.")
            else:
                lines.append("Вопрос: " + " ".join(inquiry_original_body(draft).split())[:220])
        for item in (draft.items or [])[:3]:
            title = " ".join((item.raw_product_text or "Товар").split())[:90]
            lines.append(f"• {title} — " + (
                f"{item.qty} шт." if getattr(item, "quantity_confidence", "confirmed") == "confirmed" and item.qty > 0
                else "количество не указано"))
        if len(draft.items or []) > 3:
            lines.append(f"…ещё {len(draft.items) - 3} позиций")
        if not inquiry:
            lines.extend(compact_quote(draft)[:5])
    if not inquiry:
        if tentative:
            lines.append("Сначала подтвердите: это заказ или вопрос.")
        lines += [f"Товаров: {sum(max(i.qty, 0) for i in draft.items)} шт.",
            "Сумма: " + (telegram_rubles(draft.total) if draft.total is not None else "требует проверки"),
            "Оплата: после подтверждения заказа",
            "Наличие: требует согласования", f"Сейчас: {next_step}"]
    elif not historic_order_lines:
        lines.append("Сейчас: ответить клиенту в исходной переписке Gmail")
    return "\n".join(lines)[:3900]


async def notify_managers_about_draft(draft: DraftOrder) -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Telegram token is not configured")
    async with async_session() as database_session:
        result = await database_session.execute(
            select(Manager).where(Manager.is_active.is_(True))
        )
        managers = result.scalars().all()

    if not managers:
        raise RuntimeError("No active Telegram managers")
    from app.bot.manager_group import notification_chats
    chat_ids = notification_chats(managers)
    keyboard, card = compact_draft_keyboard(draft), compact_draft_card(draft)
    from app.services.telegram_cards import enabled
    if enabled():
        from aiogram import Bot
        from aiogram.types import InlineKeyboardMarkup
        from app.services.telegram_cards import publish
        async with Bot(token=token) as bot:
            for chat_id in chat_ids:
                await publish(bot, chat_id, f"draft:{draft.id}", card,
                              InlineKeyboardMarkup.model_validate(keyboard))
        return len(chat_ids)
    def send():
        with verified_session() as http_session:
            for chat_id in chat_ids:
                response = http_session.post(f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": card, "reply_markup": keyboard}, timeout=(5, 20))
                if not response.ok:
                    raise RuntimeError(f"Telegram notification HTTP {response.status_code}")
        return len(chat_ids)
    return await asyncio.to_thread(send)
