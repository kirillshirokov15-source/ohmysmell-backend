"""No-network regression for restored unified Gmail feed and Zhanna Kim email."""
import os
import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.services.email_parser import EmailParser
from app.services.draft_telegram_service import (
    compact_draft_card, compact_draft_keyboard, build_draft_card,
    build_inquiry_message, old_inquiry_order_lines,
)


BODY = "Dsd 5.2.1 бальзам -2 шт😻 -- Отправлено из мобильной Яндекс Почты"
SUBJECT = "Еще на Сашку)"


def draft():
    return SimpleNamespace(
        id=230, source="email", status="needs_review", revision=0,
        subject=SUBJECT, customer_name="Жанна Ким",
        sender_email="customer@example.invalid", customer_type="wholesale",
        counterparty_id=None, counterparty_name=None, counterparty_candidates=[],
        contact_details={"mail_intent": "inquiry"}, total=None, items=[],
        inbound_message=SimpleNamespace(
            body_text=BODY,
            received_at=datetime(2026, 11, 6, 13, 28, tzinfo=timezone.utc),
        ),
        created_at=datetime(2026, 11, 8, 16, 39, tzinfo=timezone.utc),
    )


def main():
    for path in ("app/services/email_parser.py",
                 "app/services/draft_telegram_service.py",
                 "app/bot/telegram_bot.py"):
        ast.parse(Path(path).read_text(encoding="utf-8-sig"), filename=path)
    os.environ["OMS_EMAIL_TRIAGE_ENABLED"] = "false"
    os.environ["OMS_EMAIL_CARD_CONTEXT_ENABLED"] = "true"
    parsed = EmailParser().parse_message(SUBJECT, BODY, sender_name="Жанна Ким")
    assert parsed.intent == "order", parsed
    assert len(parsed.items) == 1, parsed
    assert parsed.items[0].qty == 2, parsed.items
    assert parsed.items[0].raw_product_text == "Dsd 5.2.1 бальзам", parsed.items
    assert EmailParser().parse_message("Вопрос по наличию", "Есть ли Dsd 5.2.1 бальзам -2 шт😻").intent == "inquiry"
    entity = draft()
    rows = old_inquiry_order_lines(entity)
    assert len(rows) == 1 and rows[0].qty == 2
    short = compact_draft_card(entity)
    detailed = build_draft_card(entity)
    assert "не заказ" not in short.lower() and "не заказ" not in detailed.lower()
    assert "Dsd 5.2.1 бальзам" in short and "2 шт." in short
    assert "06.11.2026" in short
    assert "Письмо" in build_inquiry_message(entity)
    all_buttons = compact_draft_keyboard(entity)["inline_keyboard"]
    assert any(
        b.get("callback_data") == "draft:reconsider:230"
        for row in all_buttons for b in row
    ), all_buttons
    print("UNIFIED_EMAIL_ZHANNA_SMOKE_OK", flush=True)


if __name__ == "__main__":
    main()
