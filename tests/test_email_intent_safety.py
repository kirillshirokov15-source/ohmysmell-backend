"""Gmail order vs inquiry controls (no external Gmail/MoySklad/Telegram calls)."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.integrations.email.provider import EmailMessage
from app.models.sales import CustomerType
from app.services.email_parser import EmailParser
from app.services.draft_telegram_service import build_draft_keyboard, compact_draft_card, build_draft_card
from tests.test_email_pipeline import draft_service


def email(subject, body, sender_name="Кирилл Широков"):
    return EmailMessage(
        external_message_id="test:new:1887",
        sender_email="k@example.invalid",
        sender_name=sender_name,
        subject=subject,
        body_text=body,
        received_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize("subject,body,expected", [
    ("OMS LIVE TEST 20261014 735126",
     "Marvis Classic Strong Mint 85 ml — 1 шт.\n\nКирилл Широков",
     [("Marvis Classic Strong Mint 85 ml",1)]),
    ("Заказ: Chanel Allure Homme Sport x2",
     "Спасибо!\nС уважением,\nМария Иванова",
     [("Chanel Allure Homme Sport",2)]),
    ("Заказ: Marvis Classic Strong Mint 85 ml x2; Chanel Allure Homme Sport - 1",
     "Добрый день,\nСпасибо!", [("Marvis Classic Strong Mint 85 ml",2),
     ("Chanel Allure Homme Sport",1)]),
    ("Заказ", "Прошу оформить:\n1. Marvis Classic Strong Mint 85 ml x2\n"
     "2. Chanel Allure Homme Sport 1 шт\nС уважением,\nПетр Петров",
     [("Marvis Classic Strong Mint 85 ml",2),("Chanel Allure Homme Sport",1)]),
    ("Order", "2 шт Chanel Allure Homme Sport\n"
     "Marvis Classic Strong Mint 85 ml - 3", [
         ("Chanel Allure Homme Sport",2),("Marvis Classic Strong Mint 85 ml",3)]),
])
def test_product_extraction_in_subject_and_body_without_signatures(subject,body,expected):
    parsed = EmailParser().parse_message(subject,body,sender_name="Кирилл Широков")
    assert parsed.intent == "order"
    assert [(x.raw_product_text,x.qty) for x in parsed.items] == expected


@pytest.mark.parametrize("subject,body", [
    ("Уточните цену на Marvis", "Сколько стоит Marvis Classic Strong Mint 85 ml?"),
    ("Есть ли в наличии Marvis x2?", "Подскажите сроки поставки"),
    ("Question: Chanel Allure Homme Sport 1 шт", "Can you check availability?"),
    ("Re: Заказ 128", "Здравствуйте, можно уточнить дату доставки?"),
    ("Привет", "Кирилл Широков\nСпасибо"),
])
def test_questions_are_not_orders_even_with_product_and_quantities(subject, body):
    parsed = EmailParser().parse_message(subject, body, sender_name="Кирилл Широков")
    assert parsed.intent == "inquiry"
    assert not parsed.items


def test_explicit_order_with_missing_quantity_requires_manager_review():
    parsed = EmailParser().parse_message(
        "Заказ",
        "Заказ:\n• Chanel Allure Homme Sport\n"
        "• Marvis Classic Strong Mint 85 ml - 2 шт\n"
        "С уважением,\nКирилл Широков",
        sender_name="Кирилл Широков")
    assert parsed.intent == "order"
    assert len(parsed.items) == 2
    assert parsed.items[0].raw_product_text == "Chanel Allure Homme Sport"
    assert parsed.items[0].quantity_confidence == "unknown"
    assert parsed.items[0].qty == 0
    assert parsed.items[1].qty == 2


def test_repeat_product_in_subject_and_body_not_doubled():
    parsed = EmailParser().parse_message(
        "Заказ: Marvis Classic Strong Mint 85 ml x2",
        "Marvis Classic Strong Mint 85 ml – 2 шт",
        sender_name="Кирилл Широков")
    assert [(p.raw_product_text,p.qty) for p in parsed.items] == [
        ("Marvis Classic Strong Mint 85 ml",2)]


def test_mismatched_question_cannot_finalize_or_add_catalog():
    service,repo,_ = draft_service(CustomerType.WHOLESALE, counterparty_id="cp-1")
    draft=asyncio.run(service.ingest_email(email(
        "Уточнение по цене", "Есть ли Marvis Classic Strong Mint 85 ml – 1 шт?")))
    assert draft.status == "needs_review"
    assert draft.contact_details["mail_intent"] == "inquiry"
    assert draft.items == []
    assert draft.total is None
    assert "Уточнение по почте" in compact_draft_card(draft)
    assert "Уточнение" in build_draft_card(draft)
    assert "заказ" not in compact_draft_card(draft).splitlines()[0].lower()
    keyboard = build_draft_keyboard(draft)
    assert all("finalize" not in btn["callback_data"]
               for row in keyboard["inline_keyboard"] for btn in row)


def test_signature_removed_before_matching_production_shape():
    service,repo,_ = draft_service(CustomerType.WHOLESALE, counterparty_id="cp-1")
    draft=asyncio.run(service.ingest_email(email(
        "OMS LIVE TEST 20261014 735126",
        "Marvis Classic Strong Mint 85 ml — 1 шт.\n\nКирилл Широков")))
    assert draft.contact_details["mail_intent"] == "order"
    assert len(draft.items) == 1
    assert draft.items[0].qty == 1
    assert draft.items[0].raw_product_text == "Marvis Classic Strong Mint 85 ml"
    assert "Кирилл Широков" not in [x.raw_product_text for x in draft.items]


def test_matching_options_remain_manager_controlled():
    service,repo,_=draft_service(CustomerType.WHOLESALE,counterparty_id="cp-1")
    draft=asyncio.run(service.ingest_email(email(
        "Заказ: Marvis Classic Strong Mint x1", "")))
    assert len(draft.items)==1
    assert draft.items[0].match_status == "ambiguous"
    keyboard=build_draft_keyboard(draft)
    labels=[b["text"] for row in keyboard["inline_keyboard"] for b in row]
    assert any("другой товар" in label for label in labels)
    assert all("Подтвердить заказ" not in label for label in labels)
