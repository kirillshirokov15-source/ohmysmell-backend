"""Acceptance samples based on real-world wholesale order email layouts.

Customer-entered money is documentary evidence only, not trusted selling price.
"""
import pytest
from app.services.email_parser import EmailParser
from app.integrations.email.gmail_provider import _HTMLTextExtractor

def render(html):
    p=_HTMLTextExtractor()
    p.feed(html)
    return p.text()

TABLE_RUBLES="""<html><body>
<p>Текущий баланс: Долг отсутствует</p><p>Заказ:</p>
<table><tr><th>Артикул</th><th>Наименование</th><th>Цена, ₽</th><th>Количество</th><th>Итог, ₽</th></tr>
<tr><td></td><td>chanel coco mademoiselle fresh body moisture mist 100 ml</td><td>6800</td><td>1</td><td>6800</td></tr>
<tr><td colspan="4">Итого:</td><td>6800</td></tr></table>
</body></html>"""
TABLE_DOLLARS="""<html><body>
<table><tr><th>Артикул</th><th>Наименование</th><th>Цена, $</th><th>Количество</th><th>Итог, $</th></tr>
<tr><td>CT023</td><td>charlotte tilbury glowgasm face palette — палетка хайлайтеров для лица и глаз (4 оттенка)</td><td>87.64</td><td>1</td><td>87.64</td></tr>
<tr><td>Итого:</td><td></td><td></td><td></td><td>87.64</td></tr></table></body></html>"""
TABLE_MULTI="""<html><body><p>Текущий баланс: Долг отсутствует:</p><p>Заказ:</p>
<table><tr><th>Артикул</th><th>Наименование</th><th>Цена, $</th><th>Количество</th><th>Итог, $</th></tr>
<tr><td>LG001</td><td>laura geller jelly balm hydrating lip color "in the buff" 3 g</td><td>17.18</td><td>2</td><td>34.36</td></tr>
<tr><td>LG005</td><td>laura geller high shine serum lip balm "mauve glow" 3 g</td><td>17.18</td><td>1</td><td>17.18</td></tr>
<tr><td>DRVR029</td><td>dr. vranjes rosso nobile decanter 750 ml</td><td>200.46</td><td>1</td><td>200.46</td></tr>
<tr><td>Итого:</td><td></td><td></td><td></td><td>252</td></tr></table></body></html>"""

@pytest.mark.parametrize("subject,html,skus,qty",[
    ("Заказ CEMAKOB №101499",TABLE_RUBLES,["chanel coco mademoiselle fresh body moisture mist 100 ml"],[1]),
    ("Заказ от Хуршеда",TABLE_DOLLARS,["charlotte tilbury glowgasm face palette"],[1]),
    ("От Davian Parfum",TABLE_MULTI,["laura geller jelly balm","laura geller high shine","dr. vranjes"],[2,1,1]),
])
def test_table_recognized_as_order_without_using_prices_as_quantities(subject,html,skus,qty):
    text=render(html)
    parsed=EmailParser().parse_message(subject,text,sender_name="Example")
    assert parsed.intent=="order"
    assert [x.qty for x in parsed.items]==qty
    for item, fragment in zip(parsed.items,skus):
        assert fragment in item.raw_product_text
    assert all("Итого" not in x.raw_product_text for x in parsed.items)
    assert all("Артикул" in x.quantity_evidence for x in parsed.items if "LG" in x.quantity_evidence or "CT" in x.quantity_evidence or "DRV" in x.quantity_evidence)

def test_one_line_order_sku_qty_customer_price():
    parsed=EmailParser().parse_message("Заказ","КМЦ024 1 шт 2600",sender_name="Николай")
    assert parsed.intent=="order"
    assert len(parsed.items)==1
    assert parsed.items[0].raw_product_text=="КМЦ024"
    assert parsed.items[0].qty==1
    assert "2600" in parsed.items[0].quantity_evidence

def test_question_with_named_product_is_not_order():
    parsed=EmailParser().parse_message("Уточнение","Есть ли LG001 в наличии и сколько стоит?",sender_name="Покупатель")
    assert parsed.intent=="inquiry"
    assert parsed.items==()
