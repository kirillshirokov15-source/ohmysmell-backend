"""Five-step X document preview and fail-closed buyer checks; no network writes."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.hatuna_document_preview import build_document_plan
from app.services.hatuna_fulfillment import verified_buyer_mapping
from app.services.hatuna_accounting import HatunaPreflightError
from tests.test_hatuna_readonly_check import order8, catalog, CHARLOTTE


def prepared_order():
    snapshot = order8()
    snapshot["fulfillment_status"] = "assembled"
    snapshot["revision"] = 6
    return snapshot


def test_assembled_order8_previews_five_documents_without_writes(monkeypatch):
    for flag in ("OMS_HATUNA_AUTO_WRITES_ENABLED", "OMS_HATUNA_AUTO_PROCESS_ENABLED"):
        monkeypatch.setenv(flag, "false")
    snapshot = prepared_order()
    snapshot["counterparty_id"] = None
    report = build_document_plan(snapshot, catalog(), post_pick=True, external_writes=False)
    assert "План документов · заказ №8 · БЕЗ ЗАПИСИ" in report
    assert "Сборка: assembled · ревизия 6" in report
    assert "1 × enter (внутреннее оприходование, без поставщика)" in report
    assert report.count("1 × customerorder") == 2
    assert report.count("1 × demand") == 2
    assert "2 500 ₽" in report
    assert "5 500 ₽" in report
    assert "не создаются" in report
    assert "Нет привязанного покупателя МойСклад" in report
    assert "Заказ TEST OMS" in report
    assert "Реальное проведение: ЗАПРЕЩЕНО" in report


def test_preview_never_approves_already_posted_x_stock(monkeypatch):
    monkeypatch.setenv("OMS_HATUNA_AUTO_WRITES_ENABLED", "true")
    monkeypatch.setenv("OMS_HATUNA_AUTO_PROCESS_ENABLED", "true")
    data = prepared_order()
    data["customer_name"] = "Regular client"
    updated = catalog()
    updated[CHARLOTTE]["stocks"][0]["stock"] = 1
    assert "повторный приход запрещён" in build_document_plan(
        data, updated, post_pick=True, external_writes=True)


def test_preview_requires_confirmed_assembly_and_complete_assignment():
    data = prepared_order()
    data["fulfillment_status"] = "assembling"
    data["plans"][1]["allocations"][0]["qty"] = 2
    report = build_document_plan(data, catalog(), post_pick=True, external_writes=False)
    assert "Сначала подтвердите физическую сборку" in report
    assert "Сумма количеств по складам не совпадает" in report
    assert "ЗАПРЕЩЕНО" in report


def test_preview_rejects_wrong_warehouse_and_unmapped_product():
    data = prepared_order()
    data["plans"][1]["warehouse_id"] = data["plans"][0]["warehouse_id"]
    assert "склад МойСклад не соответствует назначению" in build_document_plan(
        data, catalog(), post_pick=True)
    updated = catalog()
    del updated[CHARLOTTE]
    assert "карточка МойСклад не подтверждена" in build_document_plan(data, updated, post_pick=True)


def test_missing_buyer_blocks_receipt_before_provider_check():
    with pytest.raises(HatunaPreflightError, match="UUID"):
        asyncio.run(verified_buyer_mapping(None, environment="production"))


def test_unverified_buyer_not_accepted(monkeypatch):
    buyer_id = str(uuid4())
    for returned in ({"id": str(uuid4()), "archived": False},
                     {"id": buyer_id, "archived": True}):
        client = SimpleNamespace(get_counterparty=lambda _id, result=returned: result)
        with pytest.raises(HatunaPreflightError, match="архивирован"):
            asyncio.run(verified_buyer_mapping(buyer_id, environment="production", client=client))


def test_valid_buyer_can_pass_no_write_check():
    buyer_id = str(uuid4())
    client = SimpleNamespace(get_counterparty=lambda _id: {"id": buyer_id, "archived": False})
    assert asyncio.run(verified_buyer_mapping(
        buyer_id, environment="production", client=client)) is True


def test_provider_error_fails_closed_no_automatic_buyers():
    buyer_id = str(uuid4())
    def failure(_id):
        raise OSError("offline")
    client = SimpleNamespace(get_counterparty=failure)
    with pytest.raises(HatunaPreflightError, match="не подтверждён"):
        asyncio.run(verified_buyer_mapping(
            buyer_id, environment="production", client=client))
