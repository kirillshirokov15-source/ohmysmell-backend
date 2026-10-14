"""Shorthand products in email subject, including previously classified inquiries.

No live Gmail or MoySklad changes. Quantity is never inferred from pack size.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.services.email_parser import EmailParser
from app.services.draft_telegram_service import build_draft_keyboard, compact_draft_card
from app.services.draft_order_service import DraftOrderService
from app.models.sales import CustomerType
from app.integrations.email.provider import EmailMessage
from tests.test_email_pipeline import draft_service


@pytest.mark.parametrize("subject", [
    "dsd 001 500мл", "DSD001 500ml", "Marvis Classic Strong Mint 85 ml",
    "DSD 001 500мл", "dsd001",
])
def test_bare_sku_subject_is_tentative_never_500_pieces(subject):
    result=EmailParser().parse_message(subject,
        "С уважением,\nКирилл Широков\nexample@example.invalid",
        sender_name="Кирилл Широков")
    assert result.intent=="possible_order"
    assert len(result.items)==1
    assert result.items[0].raw_product_text==subject
    assert result.items[0].quantity_confidence=="unknown"
    assert result.items[0].qty==0


@pytest.mark.parametrize("subject,body", [
    ("Счет 001 на оплату", "С уважением, Иван"),
    ("Re: DSD 001 500мл", "Кирилл"),
    ("Вопрос про DSD001 500мл", "Сколько стоит?"),
    ("dsd 001 500мл", "Есть ли в наличии?"),
    ("Рабочая встреча 500мл", "Можем созвониться?"),
    ("Привет", "Подскажите, пожалуйста"),
])
def test_regular_correspondence_does_not_make_product_order(subject, body):
    result=EmailParser().parse_message(subject,body,sender_name="Кирилл Широков")
    assert result.intent=="inquiry"
    assert not result.items


def test_subject_only_product_creates_unfinalizable_draft_with_manager_buttons():
    service,repo,_=draft_service(CustomerType.WHOLESALE,counterparty_id="cp-1")
    msg=EmailMessage(external_message_id="gmail-bare-subject-dsd001",
        sender_email="test@example.invalid",sender_name="Кирилл Широков",
        subject="dsd 001 500мл",body_text="С уважением,\nКирилл Широков",
        received_at=datetime.now(timezone.utc))
    draft=asyncio.run(service.ingest_email(msg))
    assert draft.contact_details["mail_intent"]=="possible_order"
    assert draft.status=="needs_review"
    assert len(draft.items)==1
    item=draft.items[0]
    assert item.raw_product_text=="dsd 001 500мл"
    assert item.qty==0
    assert item.quantity_confidence=="unknown"
    assert draft.total is None
    card=compact_draft_card(draft)
    assert "Возможный заказ" in card
    assert "количество не указано" in card
    keyboard=build_draft_keyboard(draft)
    labels=[x["text"] for row in keyboard["inline_keyboard"] for x in row]
    assert "✅ Это заказ" in labels and "💬 Это вопрос" in labels
    assert any("другой товар" in text for text in labels)
    assert not any("Подтвердить заказ" in text for text in labels)
    assert "Not a confirmed" in service._finalize_problems(draft)
