"""No-network smoke for accurate source email dates and zero-item cards."""
import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import os

from app.services.draft_telegram_service import (
    compact_draft_card, build_draft_card, _mail_date_lines,
)


def fake_draft():
    return SimpleNamespace(
        id=345, source="email", status="rejected", revision=3,
        subject="Заказ", customer_name="Андрей Мирзоян",
        sender_email="shop@example.invalid", customer_type="wholesale",
        counterparty_id=None, counterparty_name=None, counterparty_candidates=[],
        contact_details={"mail_intent": "order"}, total=None, items=[],
        inbound_message=SimpleNamespace(
            received_at=datetime(2026, 10, 30, 8, 21, 42, tzinfo=timezone.utc),
            body_text="RHD028\n\nRhode Highlighting Milk 01 Pearly pink\n\n1",
        ),
        created_at=datetime(2026, 11, 8, 16, 57, tzinfo=timezone.utc),
    )


def main():
    ast.parse(Path("app/services/draft_telegram_service.py").read_text(encoding="utf-8-sig"))
    d = fake_draft()
    os.environ["OMS_EMAIL_CARD_CONTEXT_ENABLED"] = "true"
    compact = compact_draft_card(d)
    assert "30.10.2026" in compact and "08.11.2026" in compact
    assert "RHD028" in compact and "Rhode Highlighting Milk" in compact
    assert "Отклон" in compact or "отклон" in compact
    assert "Дата письма" in compact
    detailed = build_draft_card(d)
    assert "RHD028" in detailed and "Rhode Highlighting Milk" in detailed
    assert "Исходное письмо" in detailed
    os.environ["OMS_EMAIL_CARD_CONTEXT_ENABLED"] = "false"
    old = compact_draft_card(d)
    assert "RHD028" not in old and "Дата письма" not in old
    print("MAIL_CARD_DATES_AND_ORIGINAL_SMOKE_OK", flush=True)


if __name__ == "__main__":
    main()
