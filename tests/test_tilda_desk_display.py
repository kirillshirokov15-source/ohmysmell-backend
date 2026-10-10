import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import desk_display


@pytest.mark.parametrize("approved", [None, False, True])
@pytest.mark.parametrize("order_id", [None, 16])
@pytest.mark.parametrize("arithmetic_checked", [False, True])
def test_card_review_requires_approval_and_linked_order(
    monkeypatch, approved, order_id, arithmetic_checked
):
    details = {"reported_total_minor": 168000}
    if approved is not None:
        details["tilda_review_approved"] = approved
    if arithmetic_checked:
        details["tilda_price_source"] = "webhook"
    draft = SimpleNamespace(
        id=38, contact_details=details, customer_name="Synthetic", sender_email="",
        status="new" if order_id else "needs_review", total=168000 if arithmetic_checked else None,
        review_notes="Требуется согласование", items=[], revision=0,
    )
    desk = SimpleNamespace(order_id=order_id, manager_id=None, customer_telegram_id=None, stage="working")
    order = SimpleNamespace(
        status="new", payment_status="unpaid", fulfillment_status="new", delivery_status="pending"
    )
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.get.side_effect = [desk, order] if order_id else [desk]
    session.scalar.return_value = draft
    monkeypatch.setattr(desk_display, "async_session", lambda: session)
    monkeypatch.setattr(desk_display, "availability", AsyncMock(return_value={}))

    text, _ = asyncio.run(desk_display.desk_card(38))

    if approved and order_id:
        assert "Проверка суммы: арифметика согласована; одобрено менеджером" in text
        assert "Цена Tilda заявлена сайтом; одобрена менеджером" in text
        assert "требуется одобрение менеджера" not in text
        assert "одобрение менеджера обязательно" not in text
    else:
        assert "Цена Tilda заявлена сайтом; одобрение менеджера обязательно." in text
        assert "одобрено менеджером" not in text
        assert "одобрена менеджером" not in text
        if arithmetic_checked:
            assert "Проверка суммы: арифметика согласована; требуется одобрение менеджера" in text
        else:
            assert "Проверка суммы: требует проверки" in text
    session.commit.assert_not_awaited()
    session.flush.assert_not_awaited()
