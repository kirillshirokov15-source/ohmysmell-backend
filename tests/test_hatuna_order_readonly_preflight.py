"""Read-only X preflight never creates documents or changes Railway variables."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.config.settings import settings
from app.services.hatuna_accounting import HatunaPreflightError
from scripts import hatuna_order_readonly_preflight as check


def test_x_preflight_can_only_report_readiness(monkeypatch):
    monkeypatch.setattr(settings,"external_writes_enabled",False)
    monkeypatch.setenv("OMS_HATUNA_AUTO_WRITES_ENABLED","false")
    monkeypatch.setenv("OMS_HATUNA_AUTO_PROCESS_ENABLED","false")
    check_call=AsyncMock(return_value=(SimpleNamespace(),[1,2],[2],{1:object()},222,-10001))
    monkeypatch.setattr(check,"HatunaFulfillment",lambda adapter:SimpleNamespace(preflight=check_call))
    report=asyncio.run(check.inspect(7))
    assert report == {
        "order_id":7,"status":"READY_FOR_CONTROLLED_TRIAL",
        "warehouses":2,"x_warehouse_plans":1,"order_items":1,
        "no_external_writes":True,"live_posting_enabled":False,
        "note":"Preflight only; does not mean documents were created"}
    assert check_call.await_count==1


def test_preflight_fails_closed_with_external_writes(monkeypatch):
    monkeypatch.setattr(settings,"external_writes_enabled",True)
    with pytest.raises(RuntimeError,match="EXTERNAL_WRITES_ENABLED"):
        asyncio.run(check.inspect(7))


def test_preflight_reports_blocker_not_fake_readiness(monkeypatch):
    monkeypatch.setattr(settings,"external_writes_enabled",False)
    monkeypatch.setenv("OMS_HATUNA_AUTO_WRITES_ENABLED","false")
    monkeypatch.setenv("OMS_HATUNA_AUTO_PROCESS_ENABLED","false")
    call=AsyncMock(side_effect=HatunaPreflightError("Нет карточки товара МойСклад"))
    monkeypatch.setattr(check,"HatunaFulfillment",lambda adapter:SimpleNamespace(preflight=call))
    assert asyncio.run(check.inspect(7))["status"]=="BLOCKED"
