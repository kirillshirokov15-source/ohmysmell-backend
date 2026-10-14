"""Actual wholesale mailbox examples: product order, not inquiry.

The originating email may be HTML tables. Prices and currencies in email
are customer assertions; never used to override the trusted MoySklad price.
"""
from app.integrations.email.gmail_provider import _HTMLTextExtractor
from app.services.email_parser import EmailParser


def html_render(s):
    h=_HTMLTextExtractor()
    h.feed(s)
    return h.text()


def test_khurshed_html_usd_table_is_order():
    body=html_render("""
    <table><tr><th>Артикул</th><th>Наименование</th><th>Цена, $</th>
      <th>Количество</th><th>Итог, $</th></tr>
    <tr><td>CT023</td><td>charlotte tilbury glowgasm face palette — палетка хайлайтеров для лица и глаз (4 оттенка)</td>
      <td>87.64</td><td>1</td><td>87.64</td></tr>
    <tr><td>Итого:</td><td></td><td></td><td></td><td>87.64</td></tr></table>
    """)
    result=EmailParser().parse_message("Заказ от Хуршеда",body,sender_name="Ятимов Хуршед")
    assert result.intent=="order"
    assert len(result.items)==1
    assert result.items[0].qty==1
    assert "CT023" in result.items[0].quantity_evidence
    assert "charlotte tilbury" in result.items[0].raw_product_text
    assert "87.64" not in result.items[0].raw_product_text


def test_semakov_missing_article_cell_html_order():
    body=html_render("""
    <table><tr><th>Артикул</th><th>Наименование</th><th>Цена, ₽</th>
    <th>Количество</th><th>Итог, ₽</th></tr>
    <tr><td></td><td>chanel coco mademoiselle fresh body moisture mist 100 ml</td>
    <td>6800</td><td>1</td><td>6800</td></tr>
    <tr><td>Итого:</td><td></td><td></td><td></td><td>6800</td></tr></table>
    """)
    result=EmailParser().parse_message("Заказ СЕМАКОВ №101499",body,
                                       sender_name="Alexander Semakov")
    assert result.intent=="order"
    assert len(result.items)==1
    assert result.items[0].qty==1
    assert "chanel coco" in result.items[0].raw_product_text
    assert "6800" not in result.items[0].raw_product_text


def test_nikolai_one_line_sku_with_price():
    result=EmailParser().parse_message(
        "Заказ", "KMU024   1 шт   2600\n\nС уважением,\nНиколай Захильняк",
        sender_name="Николай Захильняк")
    assert result.intent=="order"
    assert [(i.raw_product_text,i.qty) for i in result.items]==[("KMU024",1)]
    assert "2600" in result.items[0].quantity_evidence


def test_davian_three_lines_and_balance_not_product():
    body=html_render("""
    <p>Текущий баланс:</p><p>Долг отсутствует:</p>
    <p>Заказ:</p>
    <table><tr><th>Артикул</th><th>Наименование</th><th>Цена, $</th><th>Количество</th><th>Итог, $</th></tr>
    <tr><td>LG001</td><td>laura geller jelly balm hydrating lip color in the buff 3 g</td>
        <td>17.18</td><td>2</td><td>34.36</td></tr>
    <tr><td>LG005</td><td>laura geller high shine serum lip balm mauve glow 3 g</td>
        <td>17.18</td><td>1</td><td>17.18</td></tr>
    <tr><td>DRVR029</td><td>dr. vranjes rosso nobile decanter 750 ml</td>
        <td>200.46</td><td>1</td><td>200.46</td></tr>
    <tr><td>Итого:</td><td></td><td></td><td></td><td>252</td></tr></table>
    """)
    result=EmailParser().parse_message("Davian Parfum",body,sender_name="Davian Parfum")
    assert result.intent=="order"
    assert [i.qty for i in result.items]==[2,1,1]
    assert len(result.items)==3
    assert all(x not in " ".join(i.raw_product_text for i in result.items).casefold()
               for x in ("баланс","долг","итого"))
    assert "DRVR029" in result.items[2].quantity_evidence


def test_question_with_fake_table_headers_is_not_order():
    question="Есть ли у вас товары из таблицы?\nАртикул\tНаименование\tЦена\tКоличество\n"
    result=EmailParser().parse_message("Вопрос о наличии",question)
    assert result.intent=="inquiry"
    assert not result.items


def test_inline_html_words_keep_spaces_and_formatting_does_not_split_cells():
    body=html_render('''
      <table><tr><th>Артикул</th><th>Наименование</th><th>Цена, ₽</th><th>Количество</th></tr>
      <tr><td>SK123</td><td><span>Chanel</span> <strong>Coco</strong> Mademoiselle</td><td>6800</td><td>1</td></tr>
      </table>
    ''')
    classified=EmailParser().parse_message("Заказ",body)
    assert classified.intent=="order"
    assert len(classified.items)==1
    assert classified.items[0].raw_product_text=="Chanel Coco Mademoiselle"
    assert classified.items[0].qty==1


def test_wholesale_exact_table_sku_matches_catalog_not_customer_price():
    import asyncio
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.integrations.email.provider import EmailMessage
    from app.models.sales import CustomerType
    from app.services.draft_order_service import DraftOrderService
    from app.services.product_matching_service import ProductMatchingService
    from app.services.price_service import PriceService
    from tests.test_email_pipeline import FakeDraftRepository, FakeCustomerService, FakeCounterpartyService

    product={"id":"moysklad-ct023","name":"Оттенки Charlotte Glowgasm",
             "article":"CT023", "salePrices":[
             {"value":555000,"priceType":{"name":"Цена продажи"}}]}
    catalog=SimpleNamespace(get_products=lambda:[product])
    service=DraftOrderService(repository=FakeDraftRepository(),
        customer_service=FakeCustomerService(CustomerType.WHOLESALE,"cp-existing"),
        matching_service=ProductMatchingService(catalog),
        counterparty_service=FakeCounterpartyService(),
        price_service=PriceService({CustomerType.WHOLESALE:"Цена продажи"}))
    body=html_render("""
    <table><tr><th>Артикул</th><th>Наименование</th><th>Цена, $</th><th>Количество</th><th>Итог, $</th></tr>
    <tr><td>CT023</td><td>charlotte tilbury glowgasm face palette</td><td>87.64</td><td>1</td><td>87.64</td></tr>
    </table>
    """)
    msg=EmailMessage(external_message_id="table-art-CT023", sender_email="buyer@example.com",
        sender_name="Хуршед", subject="Заказ от Хуршеда",body_text=body,
        received_at=datetime.now(timezone.utc))
    draft=asyncio.run(service.ingest_email(msg))
    assert len(draft.items)==1
    assert draft.items[0].raw_product_text=="charlotte tilbury glowgasm face palette"
    assert draft.items[0].product_id=="moysklad-ct023"
    assert draft.items[0].article=="CT023"
    assert draft.items[0].price==555000  # catalog price, NOT 87.64 USD
