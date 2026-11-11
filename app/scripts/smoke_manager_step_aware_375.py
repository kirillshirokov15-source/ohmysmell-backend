"""Offline regression for stage-aware Gmail cards, including production draft 375.

No real DB, MoySklad writes, payments or Telegram sends.
"""
import ast
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from app.services.draft_telegram_service import (
    build_draft_keyboard, build_item_edit_keyboard, build_item_edit_card,
    compact_draft_keyboard, compact_draft_card,
)


def draft(*, status="needs_review", qty=1, confidence="confirmed",
          matched=True, price=None, counterparty=True):
    item = SimpleNamespace(
        id=259, qty=qty, quantity_confidence=confidence,
        match_status="matched" if matched else "not_found",
        raw_product_text="DsD de Luxe 4.5 Keratin Treatment Serum 200 мл",
        product_id="product-dsd019" if matched else None,
        product_name="DsD de Luxe 4.5 Keratin Treatment Serum",
        article="DSD019" if matched else None, price=price,
        item_total=price * qty if price is not None else None,
        candidates=[], quantity_evidence="1 шт.",
    )
    return SimpleNamespace(
        id=375, revision=2, source="email", status=status,
        customer_type="wholesale", customer_name="Davian Opt",
        sender_email="wholesale@example.invalid", subject="Заказ",
        counterparty_id="counterparty123" if counterparty else None,
        counterparty_name="Davian Opt" if counterparty else None,
        counterparty_candidates=[], items=[item],
        contact_details={"mail_intent": "order"}, total=price * qty if price is not None else None,
        finalized_order_id=None, review_notes=None,
        inbound_message=SimpleNamespace(
            body_text="DsD de Luxe Keratin Treatment Serum 1 шт.",
            received_at=datetime(2026, 11, 10, 7, 49, tzinfo=timezone.utc),
        ),
        created_at=datetime(2026, 11, 11, 1, tzinfo=timezone.utc),
    )


def ids(keyboard):
    return [b["callback_data"]
            for row in keyboard["inline_keyboard"] for b in row]


def main():
    for path in ("app/services/draft_telegram_service.py", "app/bot/telegram_bot.py"):
        ast.parse(Path(path).read_text(encoding="utf-8-sig"), filename=path)
    os.environ["OMS_MANAGER_STEP_AWARE_CARDS_ENABLED"] = "true"
    os.environ["OMS_EMAIL_TRIAGE_ENABLED"] = "false"
    os.environ["OMS_EMAIL_CARD_CONTEXT_ENABLED"] = "true"
    d = draft()
    full = build_draft_keyboard(d)
    commands = ids(full)
    assert "draft:recheckprice:375:v2" in commands, commands
    assert "draft:edititems:375:v2" in commands, commands
    assert not any(":qtymanual:" in c for c in commands), commands
    assert not any(":additem:" in c for c in commands), commands
    first = compact_draft_keyboard(d)["inline_keyboard"][0][0]
    assert first["callback_data"] == "draft:recheckprice:375:v2", first
    assert "цену" in first["text"].lower()
    card = compact_draft_card(d)
    assert "Товаров: 1 шт." in card and "нет цены" in card
    assert "Опт" in card
    assert "DSD019" in build_item_edit_card(d)
    editor = ids(build_item_edit_keyboard(d))
    assert "draft:qtymanual:375:259:v2" in editor
    assert "draft:searchhint:375:259:v2" in editor
    assert "draft:additem:375:v2" in editor
    assert "draft:refresh:375" in editor
    ready = draft(status="ready", price=226000)
    assert ":finalize:" in compact_draft_keyboard(ready)["inline_keyboard"][0][0]["callback_data"]
    no_qty = draft(qty=0, confidence="unknown")
    assert ":qtymanual:" in compact_draft_keyboard(no_qty)["inline_keyboard"][0][0]["callback_data"]
    no_match = draft(matched=False)
    assert ":actions:" in compact_draft_keyboard(no_match)["inline_keyboard"][0][0]["callback_data"]
    no_counterparty = draft(counterparty=False)
    assert ":counterparty_select:" in compact_draft_keyboard(no_counterparty)["inline_keyboard"][0][0]["callback_data"]
    os.environ["OMS_MANAGER_STEP_AWARE_CARDS_ENABLED"] = "false"
    old = ids(build_draft_keyboard(d))
    assert any(":qtymanual:" in c for c in old), old
    assert any(":additem:" in c for c in old), old
    assert not any(":recheckprice:" in c for c in old), old
    print("MANAGER_STEP_AWARE_375_SMOKE_OK", flush=True)


if __name__ == "__main__":
    main()
