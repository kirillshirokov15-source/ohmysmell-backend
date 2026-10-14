"""Screenshotted wholesale Gmail layouts are orders, not "inquiry".

Fixtures are synthetic variants of real Gmail shapes. No real email/stock sends.
"""
import base64
from datetime import datetime, timezone

from app.services.email_parser import EmailParser
from app.integrations.email.gmail_provider import GmailEmailProvider
from app.integrations.email.provider import EmailMessage
from app.models.sales import CustomerType
from tests.test_email_pipeline import draft_service
import asyncio


def table(*rows):
    return ("<html><body><table border='1'>" +
            "".join("<tr>" + "".join(f"<td>{v}</td>" for v in row) + "</tr>"
                    for row in rows) + "</table></body></html>")


def mime(kind, txt):
    return {"mimeType": kind, "body": {
        "data": base64.urlsafe_b64encode(txt.encode("utf-8")).decode("ascii")}}


def expect(subject, body, quantities, *, sender="Test Customer"):
    result=EmailParser().parse_message(subject,body,sender_name=sender)
    assert result.intent=="order"
    assert [(x.raw_product_text,x.qty) for x in result.items]==quantities
    return result


def test_html_sku_price_usd_table_is_order_not_inquiry():
    html=table(
        ("Артикул","Наименование","Цена, $","Количество","Итог, $"),
        ("CT023","charlotte tilbury glowgasm face palette – палетка хайлайтеров","87.64","1","87.64"),
        ("Итого:","","","","87.64"))
    body=GmailEmailProvider._body_text(mime("text/html",html))
    result=expect("Заказ от клиента",body,[("charlotte tilbury glowgasm face palette – палетка хайлайтеров",1)])
    assert result.items[0].quantity_evidence.startswith("Артикул CT023")
    assert "87.64" in result.items[0].quantity_evidence


def test_html_rub_table_without_sku_is_order():
    html=table(
        ("Артикул","Наименование","Цена, ₽","Количество","Итог, ₽"),
        ("","chanel coco mademoiselle fresh body moisture mist 100 ml","6800","1","6800"),
        ("Итого:","","","","6800"))
    body=GmailEmailProvider._body_text(mime("text/html",html))
    expect("Заказ СЕМАКОВ №101499",body,[
        ("chanel coco mademoiselle fresh body moisture mist 100 ml",1)])


def test_short_sku_quantity_rubles_not_swapped():
    result=expect("Заказ","КМЦ024   1 шт   2600",[("КМЦ024",1)])
    assert "2600" in result.items[0].quantity_evidence
    assert result.items[0].quantity_confidence=="confirmed"


def test_balance_then_multiline_wholesale_usd_table():
    head="<p>Текущий баланс:</p><p>Долг отсутствует.</p><p>Заказ:</p>"
    html=head+table(
        ("Артикул","Наименование","Цена, $","Количество","Итог, $"),
        ("LG001","laura geller jelly balm hydrating lip color 3 g","17.18","2","34.36"),
        ("LG005","laura geller high shine serum lip balm 3 g","17.18","1","17.18"),
        ("DRVR029","dr. vranjes rosso nobile decanter 750 ml","200.46","1","200.46"),
        ("Итого:","","","","252"))
    body=GmailEmailProvider._body_text(mime("text/html",html))
    result=expect("Заказ",body,[
        ("laura geller jelly balm hydrating lip color 3 g",2),
        ("laura geller high shine serum lip balm 3 g",1),
        ("dr. vranjes rosso nobile decanter 750 ml",1)])
    assert len(result.items)==3


def test_prefers_html_table_to_multipart_flattened_plaintext():
    html=table(("Артикул","Наименование","Цена, $","Количество","Итог, $"),
               ("CT023","charlotte tilbury palette 4 shade","87.64","1","87.64"))
    payload={"mimeType":"multipart/alternative", "parts":[
        mime("text/plain","Артикул Наименование Цена, $ Количество Итого, $\nCT023 charlotte tilbury palette 4 shade 87.64 1 87.64"),
        mime("text/html",html)]}
    body=GmailEmailProvider._body_text(payload)
    assert "\t" in body
    expect("Заказ от клиента",body,[("charlotte tilbury palette 4 shade",1)])


def test_question_and_signature_never_become_purchase():
    body="Подскажите, есть ли в наличии CT023 и какая цена?\nС уважением, Иван"
    parsed=EmailParser().parse_message("Уточнение по товару",body,sender_name="Иван")
    assert parsed.intent=="inquiry"
    assert parsed.items==()


def test_gmail_draft_prices_from_moysklad_not_currency_in_email():
    service,repo,_=draft_service(CustomerType.WHOLESALE,counterparty_id="cp-1")
    message=EmailMessage(external_message_id="formatted-ct023-no-network",
        sender_email="customer@example.invalid", sender_name="Test Customer",
        subject="Заказ от клиента",
        body_text=GmailEmailProvider._body_text(mime("text/html", table(
          ("Артикул","Наименование","Цена, $","Количество","Итог, $"),
          ("CT023","charlotte tilbury glowgasm face palette","87.64","1","87.64")))),
        received_at=datetime.now(timezone.utc))
    draft=asyncio.run(service.ingest_email(message))
    assert draft.contact_details["mail_intent"]=="order"
    assert len(draft.items)==1
    assert draft.items[0].qty==1
    # Without a verified catalog match there is no automatic conversion of
    # an email $ price into an incorrect ruble selling price.
    if draft.items[0].match_status!="matched":
        assert draft.items[0].price is None
