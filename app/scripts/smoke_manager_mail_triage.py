"""Offline smoke: distinct customer email streams, safe technical archive."""
import os
import ast
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import dialect
from app.models.draft_order import DraftOrder
from app.repositories.draft_order_repository import _customer_mail_filter
from app.services.email_triage import (
    is_technical_notice, kind_for_draft, should_send_order_notification,
)
from app.bot.manager_dialog import fallback
from app.services.draft_telegram_service import compact_draft_card, compact_draft_keyboard


def draft(intent, *, sender="customer@example.invalid", subject="Здравствуйте", id_=1):
    return SimpleNamespace(
        id=id_, source="email", status="needs_review", revision=0,
        subject=subject, customer_name="Клиент", sender_email=sender,
        contact_details={"mail_intent": intent}, total=None, items=[],
        customer_type="wholesale", counterparty_id=None,
        counterparty_name=None, counterparty_candidates=[],
        inbound_message=SimpleNamespace(received_at=datetime(2026, 11, 5, 12, tzinfo=timezone.utc),
                                         body_text="Здравствуйте, нужна консультация"),
        created_at=datetime(2026, 11, 8, 12, tzinfo=timezone.utc),
    )


def main():
    for path in ["app/services/email_triage.py",
                 "app/services/draft_telegram_service.py",
                 "app/repositories/draft_order_repository.py",
                 "app/bot/telegram_bot.py",
                 "app/bot/manager_dialog.py"]:
        ast.parse(Path(path).read_text(encoding="utf-8-sig"), filename=path)
    os.environ["OMS_EMAIL_TRIAGE_ENABLED"] = "true"
    question = draft("inquiry", id_=101)
    order = draft("order", id_=102)
    possible = draft("possible_order", id_=103)
    tech = draft("inquiry", id_=104,
                 sender="mailer-daemon@googlemail.com",
                 subject="Delivery Status Notification (Failure)")
    assert kind_for_draft(question) == "question"
    assert kind_for_draft(order) == "order"
    assert kind_for_draft(possible) == "order"
    assert kind_for_draft(tech) == "technical"
    assert not should_send_order_notification(question)
    assert should_send_order_notification(order)
    assert should_send_order_notification(possible)
    assert not should_send_order_notification(tech)
    assert not is_technical_notice("person@example.com", tech.subject)
    assert "🛠" in compact_draft_card(tech)
    assert "Товаров:" not in compact_draft_card(tech)
    buttons = compact_draft_keyboard(tech)["inline_keyboard"]
    assert len(buttons) == 1 and buttons[0][0]["callback_data"] == "draft:details:104"
    nav = fallback(898019732, more=True)
    assert any("Вопросы с почты" in b.text
               for row in nav.inline_keyboard for b in row)
    for kind in ["orders", "questions", "technical"]:
        stmt = select(DraftOrder.id).where(_customer_mail_filter(kind))
        query = str(stmt.compile(dialect=dialect()))
        assert "contact_details" in query and "WHERE" in query
    os.environ["OMS_EMAIL_TRIAGE_ENABLED"] = "false"
    assert should_send_order_notification(question)
    old_nav = fallback(898019732, more=True)
    assert not any("Вопросы с почты" in b.text
                   for row in old_nav.inline_keyboard for b in row)
    print("MANAGER_EMAIL_TRIAGE_SMOKE_OK", flush=True)


if __name__ == "__main__":
    main()
