"""Read-only manager diagnostic for the actual shape of Tilda order 8."""
from copy import deepcopy
from uuid import uuid4

from app.services.hatuna_readonly_check import evaluate


OWN = str(uuid4())
X = str(uuid4())
BOADICEA = str(uuid4())
CHARLOTTE = str(uuid4())
ORG = str(uuid4())
CUSTOMER = str(uuid4())


def order8():
    return {
        "order_id": 8, "revision": 4, "status": "new",
        "source": "website", "manual_fulfillment": True,
        "needs_review": False, "customer_name": "TEST OMS CRM",
        "comment": "", "fulfillment_status": "new",
        "delivery_method": "manual",
        "counterparty_id": CUSTOMER, "organization_id": ORG,
        "warehouse_ids": (OWN, X),
        "items": {
            11: {"product_id": BOADICEA, "name": "Boadicea Ardent Set",
                 "qty": 1, "sale_price_minor": 550000},
            12: {"product_id": CHARLOTTE, "name": "Charlotte Tilbury 100 мл",
                 "qty": 1, "sale_price_minor": 350000},
        },
        "plans": [
            {"id": 8, "status": "planned", "warehouse_id": OWN,
             "allocations": [{"item_id": 11, "qty": 1, "source_type": "own",
                              "status": "confirmed"}]},
            {"id": 9, "status": "planned", "warehouse_id": X,
             "allocations": [{"item_id": 12, "qty": 1, "source_type": "partner_x",
                              "status": "awaiting_receipt"}]},
        ],
        "operations": {},
    }


def catalog():
    return {
        BOADICEA: {"id": BOADICEA, "stocks": [
            {"id": OWN, "name": "Основной склад 3143",
             "stock": 1, "reserve": 0, "available": 1}]},
        CHARLOTTE: {"id": CHARLOTTE, "stocks": [
            {"id": X, "name": "Склад Х",
             "stock": 0, "reserve": 0, "available": 0}]},
    }


def test_order8_reports_waiting_assembly_and_never_claims_live_posting():
    report = evaluate(order8(), catalog(), post_pick=False,
                      auto_process=False, external_writes=False)
    assert "Проверка склада Х · заказ №8" in report
    assert "план №9: документ не создан" in report
    assert "2 500" in report and "расчётная" in report
    assert "Сценарий post-pick" in report
    assert "Тестовый заказ TEST OMS" in report
    assert "Реальные складские операции: ПОКА ЗАПРЕЩЕНЫ" in report


def test_existing_x_stock_blocks_duplicate_receipt_even_with_all_feature_flags():
    order = order8()
    order["customer_name"] = "Real Customer"
    order["fulfillment_status"] = "assembled"
    current = catalog()
    current[CHARLOTTE]["stocks"][0]["stock"] = 1
    report = evaluate(order, current, post_pick=True,
                      auto_process=True, external_writes=True)
    assert "уже есть учтённый остаток/резерв" in report
    assert "Пока не подтверждена сборка" not in report
    assert "ПОКА ЗАПРЕЩЕНЫ" in report


def test_missing_matching_product_refuses_posting_without_heuristics():
    wrong = catalog()
    del wrong[CHARLOTTE]
    report = evaluate(order8(), wrong, post_pick=True,
                      auto_process=False, external_writes=False)
    assert "нет подтверждённой карточки товара МойСклад" in report


def test_pending_journal_operation_for_x_blocks_repeat():
    order = order8()
    from app.services.hatuna_accounting import hatuna_operation_keys
    key = hatuna_operation_keys(8, 9)["receipt"]
    order["operations"][key] = "uncertain"
    report = evaluate(order, catalog(), post_pick=True,
                      auto_process=True, external_writes=False)
    assert "журнал=uncertain" in report
    assert "повтор запрещён" in report


def test_wrong_warehouse_and_underallocated_goods_are_rejected():
    order = order8()
    order["plans"][1]["warehouse_id"] = OWN
    order["plans"][1]["allocations"][0]["qty"] = 2
    report = evaluate(order, catalog())
    assert "не найден у товара" in report
    assert "превышает заказ" in report


def test_zero_report_stock_does_not_claim_physical_receipt():
    report = evaluate(order8(), catalog(), post_pick=True,
                      auto_process=False, external_writes=False)
    assert "Фактическое нахождение товара подтверждает только сборщик" in report
    assert "Фактическое получение подтверждено" not in report
