"""Prices and currencies quoted by wholesale email must be auditable, never trusted."""
import asyncio
from datetime import datetime, timezone

from app.services.email_parser import EmailParser
from app.integrations.email.gmail_provider import _HTMLTextExtractor
from app.integrations.email.provider import EmailMessage
from app.models.sales import CustomerType
from tests.test_email_pipeline import draft_service


def render(html):
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    return extractor.text()


def test_davian_valid_usd_table_arithmetic_and_sku():
    body = render("""
    <table><tr><th>Артикул</th><th>Наименование</th><th>Цена, $</th><th>Количество</th><th>Итого, $</th></tr>
    <tr><td>LG001</td><td>Laura Geller Lip</td><td>17.18</td><td>2</td><td>34.36</td></tr>
    <tr><td>LG005</td><td>Laura Geller Balm</td><td>17.18</td><td>1</td><td>17.18</td></tr>
    <tr><td>DRVR029</td><td>Dr. Vranjes Rosso</td><td>200.46</td><td>1</td><td>200.46</td></tr>
    <tr><td>Итого:</td><td></td><td></td><td></td><td>252</td></tr></table>
    """)
    result = EmailParser().parse_message("Заказ", body)
    assert result.intent == "order"
    assert result.quote_issues == ()
    assert result.quote_reported_total == "252"
    assert [(i.quoted_article, i.quoted_unit_price, i.quoted_row_total,
             i.quoted_currency) for i in result.items] == [
                ("LG001", "17.18", "34.36", "USD"),
                ("LG005", "17.18", "17.18", "USD"),
                ("DRVR029", "200.46", "200.46", "USD"),
            ]


def test_table_line_total_and_grand_total_mismatch_are_review_issues():
    body = ("Артикул\tНаименование\tЦена, $\tКоличество\tИтого, $\n"
            "LG001\tLaura Lip\t17.18\t2\t35.00\n"
            "Итого:\t\t\t\t36.00")
    result = EmailParser().parse_message("Заказ", body)
    assert "line_total_mismatch" in result.quote_issues
    assert "grand_total_mismatch" in result.quote_issues


def test_short_sku_price_without_currency_not_assumed_rubles():
    result = EmailParser().parse_message("Заказ", "KMU024 1 шт 2600")
    assert result.items[0].quoted_unit_price == "2600"
    assert result.items[0].quoted_currency is None
    assert result.quote_issues == ("currency_unknown",)


def test_draft_stores_quote_audit_but_never_prices_from_unverified_email():
    service, _, _ = draft_service(CustomerType.WHOLESALE, counterparty_id="cp-1")
    message = EmailMessage(
        external_message_id="quote-audit-sample-new",
        sender_email="buyer@example.invalid", sender_name="Customer",
        subject="Заказ", body_text="KMU024 1 шт 2600",
        received_at=datetime.now(timezone.utc),
    )
    draft = asyncio.run(service.ingest_email(message))
    audit = draft.contact_details["mail_quote_audit"]
    assert audit["items"][0]["unit_price"] == "2600"
    assert audit["items"][0]["currency"] is None
    assert "currency_unknown" in audit["issues"]
    assert str(draft.status) == "needs_review"
