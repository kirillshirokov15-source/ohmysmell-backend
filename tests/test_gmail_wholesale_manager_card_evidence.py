"""Real wholesale Gmail samples in Telegram, preserving customer quote evidence."""
from types import SimpleNamespace

from app.services.email_order_quote import compact_quote, full_quote, quote_lines
from app.services.draft_telegram_service import compact_draft_card, compact_draft_keyboard
from app.services.email_parser import EmailParser
from app.integrations.email.gmail_provider import _HTMLTextExtractor


def extract(html):
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.text()


def draft(subject, body, sender="Test customer"):
    parsed = EmailParser().parse_message(subject, body, sender_name=sender)
    lines = [SimpleNamespace(id=i+1, raw_product_text=x.raw_product_text,
        qty=x.qty, quantity_confidence=x.quantity_confidence,
        quantity_evidence=x.quantity_evidence, price=None, item_total=None,
        match_status="not_found", product_id=None, candidates=[])
        for i, x in enumerate(parsed.items)]
    return SimpleNamespace(id=45, source="email", customer_name=sender,
        sender_email="test@example.invalid", subject=subject,
        customer_type="wholesale", status="needs_review", revision=2,
        contact_details={"mail_intent": parsed.intent},
        counterparty_id=None, counterparty_name=None, counterparty_candidates=[],
        items=lines, inbound_message=SimpleNamespace(body_text=body),
        total=None, problems=["Товар требует сопоставления"])


def table(*rows):
    return extract("<html><body><p>Текущий баланс: долг отсутствует</p><table>"+
      "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
              for row in rows)+"</table></body></html>")


def test_khurshed_quoted_dollars_separate_from_crm_rubles():
    d=draft("Заказ от Хуршеда", table(
        ("Артикул","Наименование","Цена, $","Количество","Итого, $"),
        ("CT023","Charlotte Tilbury Glowgasm Face Palette","87.64","1","87.64"),
        ("Итого:","","","","87.64")))
    info=quote_lines(d)
    assert len(info["lines"])==1
    assert info["lines"][0]["article"]=="CT023"
    assert info["lines"][0]["unit_minor"]==8764
    assert info["currency"]=="USD"
    assert info["total_minor"]==8764
    assert not info["discrepancies"]
    card=compact_draft_card(d)
    assert "Оптовый заказ из почты" in card
    assert "Артикул из письма: CT023" in card
    assert "87,64 USD" in card
    assert "Сумма: требует проверки" in card
    assert "сверк" not in card.lower()
    keyboard=compact_draft_keyboard(d)
    assert "Проверить заказ" in keyboard["inline_keyboard"][0][0]["text"]


def test_semakov_missing_sku_still_valid_order_rub_price_not_crm_total():
    d=draft("Заказ СЕМАКОВ №101499", table(
        ("Артикул","Наименование","Цена, ₽","Количество","Итого, ₽"),
        ("","Chanel Coco Mademoiselle Fresh Body Moisture Mist 100 ml","6800","1","6800"),
        ("Итого:","","","","6800")))
    report=quote_lines(d)
    assert report["lines"][0]["article"] is None
    assert report["lines"][0]["unit_minor"]==680000
    assert report["currency"]=="RUB"
    assert "6 800,00 RUB" in compact_draft_card(d)


def test_davian_balance_is_not_item_and_usd_arithmetic_is_exact():
    d=draft("Davian Parfum", table(
        ("Артикул","Наименование","Цена, $","Количество","Итого, $"),
        ("LG001","Laura Geller Jelly Balm Hydrating Lip Color","17.18","2","34.36"),
        ("LG005","Laura Geller High Shine Serum Lip Balm","17.18","1","17.18"),
        ("DRVR029","Dr. Vranjes Rosso Nobile Decanter 750 ml","200.46","1","200.46"),
        ("Итого:","","","","252")))
    res=quote_lines(d)
    assert len(d.items)==3
    assert len(res["lines"])==3
    assert res["total_minor"]==25200
    assert res["calculated_minor"]==25200
    assert not res["discrepancies"]
    assert "Текущий баланс" not in " ".join(i.raw_product_text for i in d.items)
    assert "252,00 USD" in compact_draft_card(d)
    assert "Артикул: DRVR029" in "\n".join(full_quote(d))


def test_mismatched_line_and_total_warn_manager_without_modifying_draft():
    d=draft("Заказ", table(
        ("Артикул","Наименование","Цена, $","Количество","Итого, $"),
        ("LG001","Laura Geller","17.18","2","99.00"),
        ("Итого:","","","","100.00")))
    info=quote_lines(d)
    assert info["discrepancies"]
    assert "Арифметика письма не совпадает" in compact_draft_card(d)
    assert d.total is None


def test_short_sku_quote_unknown_currency_stays_unknown():
    d=draft("Заказ","KMU024 1 шт 2600\nС уважением, тест")
    info=quote_lines(d)
    assert info["lines"][0]["article"]=="KMU024"
    assert info["lines"][0]["currency"] is None
    assert "(валюта не определена)" in compact_draft_card(d)
    assert d.items[0].qty==1


def test_product_volume_in_subject_has_unknown_qty_not_500_units():
    d=draft("dsd 001 500мл", "С уважением, клиент")
    card=compact_draft_card(d)
    assert "Возможный заказ" in card
    assert "количество не указано" in card
    assert "500 шт." not in card


def test_true_inquiry_is_not_mislabelled_order():
    d=draft("Вопрос", "Есть ли LG001 в наличии и какая цена?")
    card=compact_draft_card(d)
    assert "Уточнение по почте · не заказ" in card
    assert "Оптовый заказ из почты" not in card


def test_old_inquiry_with_structured_order_is_recoverable_not_duplicated():
    d=draft("Заказ от Хуршеда", table(
        ("Артикул","Наименование","Цена, $","Количество","Итого, $"),
        ("CT023","Charlotte Tilbury Glowgasm Face Palette","87.64","1","87.64")))
    d.items=[]
    d.contact_details={"mail_intent":"inquiry"}
    from app.services.draft_telegram_service import old_inquiry_order_lines
    candidate=old_inquiry_order_lines(d)
    assert len(candidate)==1 and candidate[0].qty==1
    card=compact_draft_card(d)
    assert "Сохранённое письмо содержит заказ" in card
    assert "Charlotte Tilbury" in card
    assert "«Разобрать как заказ»" in card
    kb=compact_draft_keyboard(d)
    assert ":reconsider:" in kb["inline_keyboard"][0][0]["callback_data"]
    assert kb["inline_keyboard"][0][0]["callback_data"].endswith(":v2")
    assert d.items==[]  # preview did not create a second order or alter draft


def test_old_question_is_never_auto_reclassified():
    d=draft("Вопрос по цене","Сколько стоит Chanel Coco Mademoiselle?")
    d.contact_details={"mail_intent":"inquiry"}
    from app.services.draft_telegram_service import old_inquiry_order_lines
    assert old_inquiry_order_lines(d)==()
    card=compact_draft_card(d)
    assert "Уточнение по почте · не заказ" in card
    assert "Сохранённое письмо содержит заказ" not in card
