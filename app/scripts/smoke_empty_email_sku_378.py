"""No-network regression for SKU-only wholesale email and editable empty order."""
import ast
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.services.email_parser import EmailParser
from app.services.draft_telegram_service import (
    build_draft_keyboard, compact_draft_keyboard, compact_draft_card,
)
from app.services.draft_order_service import DraftOrderService
from app.models.draft_order import ProductMatchStatus


def draft(items=None):
    return SimpleNamespace(
        id=378, revision=0, status="needs_review", source="email",
        subject="Заказ", customer_name="Николай", sender_email="buyer@example.invalid",
        customer_type="wholesale", counterparty_id="linked", counterparty_name="Покупатель",
        counterparty_candidates=[], contact_details={"mail_intent": "order"},
        items=items if items is not None else [], total=None,
        finalized_order_id=None,
        inbound_message=SimpleNamespace(
            body_text="KMU024",
            received_at=datetime(2026, 11, 10, 9, 32, tzinfo=timezone.utc),
        ),
        created_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
    )


class FakeMatcher:
    async def match_async(self, name, qty):
        assert name in {"KMU024", "KMU024 – 2 шт"} or name.startswith("KMU")
        return SimpleNamespace(
            status=ProductMatchStatus.MATCHED,
            product={"id": "moy-123", "name": "Товар из МойСклад", "article": "KMU024"},
            candidates=(),
        )


class FakeRepo:
    def __init__(self, value):
        self.value = value
        self.calls = []

    async def get(self, draft_id):
        assert draft_id == 378
        return self.value

    async def add_email_items(self, draft_id, rows, actor, from_original):
        self.calls.append((draft_id, rows, actor, from_original))
        self.value.items.extend([SimpleNamespace(
            id=100 + i, **row,
        ) for i, row in enumerate(rows)])
        self.value.revision += 1
        return self.value


async def check_service():
    source = draft()
    repo = FakeRepo(source)
    service = DraftOrderService(
        repository=repo, matching_service=FakeMatcher(),
        customer_service=object(), counterparty_service=object(),
        price_service=object(), notifier=object(),
    )
    async def fake_review(draft_id, *args):
        return source
    service.review = fake_review
    result = await service.recover_missing_email_items(378, 898019732)
    assert result is source
    assert repo.calls[0][3] is True
    item = source.items[0]
    assert item.qty == 0 and item.quantity_confidence == "unknown"
    assert item.product_id == "moy-123"
    primary = compact_draft_keyboard(source)["inline_keyboard"][0][0]
    assert ":qtymanual:" in primary["callback_data"]
    assert "Количество: требуется подтвердить" in compact_draft_card(source)
    try:
        await service.recover_missing_email_items(378, 898019732)
    except Exception:
        pass
    else:
        raise AssertionError("Cannot recover twice")
    source2 = draft()
    repo2 = FakeRepo(source2)
    service2 = DraftOrderService(
        repository=repo2, matching_service=FakeMatcher(),
        customer_service=object(), counterparty_service=object(),
        price_service=object(), notifier=object(),
    )
    service2.review = fake_review
    await service2.add_manual_email_item(378, "KMU024", 898019732)
    assert repo2.calls[0][3] is False
    assert repo2.calls[0][1][0]["qty"] == 0


def main():
    for path in (
        "app/services/email_parser.py",
        "app/services/draft_telegram_service.py",
        "app/services/draft_order_service.py",
        "app/repositories/draft_order_repository.py",
        "app/bot/manager_inputs.py",
        "app/bot/telegram_bot.py",
    ):
        ast.parse(Path(path).read_text(encoding="utf-8-sig"), filename=path)
    os.environ["OMS_EMAIL_CARD_CONTEXT_ENABLED"] = "true"
    os.environ["OMS_EMAIL_TRIAGE_ENABLED"] = "false"
    base = draft()
    parsed = EmailParser().parse_message("Заказ", "KMU024", sender_name="Николай")
    assert parsed.intent == "order" and len(parsed.items) == 1
    assert parsed.items[0].qty == 0 and parsed.items[0].quantity_confidence == "unknown"
    all_buttons = [
        button for row in build_draft_keyboard(base)["inline_keyboard"] for button in row
    ]
    callbacks = [b["callback_data"] for b in all_buttons]
    assert any(":reconsider:378" in c for c in callbacks)
    assert any(":additem:378" in c for c in callbacks)
    assert any(":reject:378" in c for c in callbacks)
    compact = compact_draft_keyboard(base)["inline_keyboard"]
    assert ":reconsider:" in compact[0][0]["callback_data"]
    assert "KMU024" in compact_draft_card(base)
    assert "Количество: требуется подтвердить" in compact_draft_card(base)
    assert EmailParser().parse_message("Есть ли KMU024?", "KMU024").intent == "inquiry"
    asyncio.run(check_service())
    print("EMPTY_EMAIL_SKU_378_SMOKE_OK", flush=True)


if __name__ == "__main__":
    main()
