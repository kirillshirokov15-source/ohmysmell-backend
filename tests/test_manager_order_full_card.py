"""Complete primary order card and safe mapped X receipt preflight."""
from types import SimpleNamespace as Obj

from app.services.manager_workspace import order_card, order_keyboard
from app.services.x_warehouse_preview import real_moysklad_id


def test_order_card_shows_buyer_contact_original_site_item_and_x_readiness():
    item = Obj(id=101, name="MoySklad Boadicea Ardent", qty=1,
               price=550000, item_total=550000)
    allocated = Obj(id=91, order_item_id=101, qty=1, managed_source=True,
                    source_type="partner_x", supply_status="awaiting_receipt",
                    receipt_reference=None, receipt_simulated=False,
                    buying_purchase=None, buying_purchase_id=None,
                    supplier_id=1)
    shipment = Obj(id=4, warehouse_id="x", status="planned", allocations=[allocated])
    order = Obj(id=5, customer_name="QA Manager", phone="+381640000000",
                source="website", items=[item], total=550000,
                payment_status="unpaid", fulfillment_status="new",
                delivery_status="pending", delivery_method="unselected", needs_review=False,
                status="new", manual_fulfillment=True, revision=3)
    text = order_card(order, [shipment],
        site_titles={101: "Boadicea The Victorious Ardent Set · soap + shampoo"})
    for label in ("Заказ №5", "Клиент: QA Manager", "Телефон: +381640000000",
                  "Канал: Сайт", "Источник товара: Склад Х",
                  "Boadicea The Victorious", "МойСклад: MoySklad Boadicea",
                  "Статус: Новый", "Итого: 5 500 ₽",
                  "Готово к сборке: 0 из 1 шт.", "оприходовать в МойСклад"):
        assert label in text
    assert "Подробнее" not in str(order_keyboard(order, [shipment]))
    assert len(text.encode("utf-16-le")) // 2 < 4096


def test_assigned_own_stock_is_ready_for_assembly():
    item = Obj(id=102, name="Продукт", qty=2, price=10000, item_total=20000)
    allocation = Obj(order_item_id=102, qty=2, managed_source=True,
                     source_type="own", supply_status="confirmed",
                     receipt_reference=None, receipt_simulated=False,
                     buying_purchase=None)
    shipment = Obj(status="planned", allocations=[allocation])
    order = Obj(id=8, customer_name="Клиент", phone="нет",
                source="website", items=[item], total=20000,
                payment_status="unpaid", fulfillment_status="new",
                delivery_status="pending", needs_review=False,
                status="new", manual_fulfillment=True, revision=1)
    content = order_card(order, [shipment])
    assert "Готово к сборке: 2 из 2 шт." in content
    assert "Источник товара: Свой склад" in content
    assert "проверить резерв МойСклад" not in content  # next step is to create it


def test_original_site_names_only_used_as_search_hints_not_provider_id():
    assert real_moysklad_id("ef902edc-c856-11f0-0a80-00b00020eed4")
    assert not real_moysklad_id("oms:partner-x:singleton")
    assert not real_moysklad_id("Склад Х")
    assert not real_moysklad_id(None)
