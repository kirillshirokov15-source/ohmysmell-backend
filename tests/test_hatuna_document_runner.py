"""Durable local Hatuna accounting executor tests; external writes disabled."""
import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config.settings import settings
from app.integrations.moysklad.documents import FakeDocumentAdapter
from app.models.fulfillment import ExternalOperation
from app.services import external_operation_service as ledger
from app.services.external_operation_service import ExternalOperationConflict
from app.services.hatuna_accounting import HatunaItem, HatunaPreflightError
from app.services.hatuna_document_runner import HatunaDocumentRunner
from tests.test_tilda_sku_review import database, catalog
from tests.test_tilda_orders import tilda_config


def kwargs():
    ids = [str(uuid4()) for _ in range(5)]
    return {
        "order_id": 101,
        "x_plan_id": 202,
        "organization_id": ids[0],
        "hatuna_id": ids[1],
        "x_store_id": ids[2],
        "items": [
            HatunaItem(ids[3], 2, 550_000, 3001),
            HatunaItem(ids[4], 1, 910_000, 3002),
        ],
        "picker_confirmed": True,
    }


def test_postpick_receipt_is_journaled_replay_safe_and_unchanged_price(database, monkeypatch):
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "external_writes_enabled", False)
    monkeypatch.setattr(ledger, "async_session", database)
    monkeypatch.setattr(ledger, "transaction_lock", AsyncMock())
    adapter = FakeDocumentAdapter()
    service = HatunaDocumentRunner(adapter=adapter)
    inputs = kwargs()

    async def run():
        result = await service.apply(**inputs)
        assert result["simulated"]
        assert result["provisional_cost"]
        assert result["purchase_order_id"] != result["receipt_id"]
        assert result["receipt_key"] != result["purchase_key"]
        assert adapter.calls == 2
        repeated = await service.apply(**inputs)
        assert repeated == result
        assert adapter.calls == 2
        async with database() as session:
            rows = (await session.scalars(select(ExternalOperation))).all()
            assert len(rows) == 2
            assert all(row.status == "succeeded" for row in rows)
            order = next(row for row in rows if row.payload["entity"] == "purchaseorder")
            receipt = next(row for row in rows if row.payload["entity"] == "supply")
            assert receipt.payload["document"]["purchaseOrder"]["meta"]["href"].endswith(
                "/purchaseorder/" + result["purchase_order_id"])
            assert order.payload["document"]["positions"] == receipt.payload["document"]["positions"]
            assert [i["price"] for i in order.payload["document"]["positions"]] == [450_000, 810_000]
    asyncio.run(run())


def test_no_docs_before_picker_confirms(database, monkeypatch):
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "external_writes_enabled", False)
    monkeypatch.setattr(ledger, "async_session", database)
    adapter = FakeDocumentAdapter()
    async def run():
        with pytest.raises(HatunaPreflightError):
            await HatunaDocumentRunner(adapter=adapter).apply(**{**kwargs(), "picker_confirmed": False})
        assert adapter.calls == 0
        async with database() as session:
            assert not (await session.scalars(select(ExternalOperation))).all()
    asyncio.run(run())


def test_ambiguous_response_must_not_duplicate_real_or_simulated_receipt(database, monkeypatch):
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(ledger, "async_session", database)
    monkeypatch.setattr(ledger, "transaction_lock", AsyncMock())
    adapter = FakeDocumentAdapter()
    adapter.lose_response = True
    service = HatunaDocumentRunner(adapter=adapter)
    inputs = kwargs()
    async def run():
        with pytest.raises(TimeoutError):
            await service.apply(**inputs)
        assert adapter.calls == 1  # purchase may already exist provider-side
        adapter.lose_response = False
        with pytest.raises(ExternalOperationConflict):
            await service.apply(**inputs)
        assert adapter.calls == 1  # no blind retries
        async with database() as session:
            rows = (await session.scalars(select(ExternalOperation))).all()
            assert len(rows) == 1 and rows[0].status == "uncertain"
    asyncio.run(run())


def test_live_executor_fails_closed_even_if_global_writes_enabled(database, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "external_writes_enabled", True)
    monkeypatch.delenv("OMS_HATUNA_AUTO_WRITES_ENABLED", raising=False)
    # Adapter constructor creates no HTTP requests.
    async def run():
        with pytest.raises(HatunaPreflightError, match="не активирована"):
            await HatunaDocumentRunner().apply(**kwargs())
    asyncio.run(run())


def test_live_cannot_run_without_persisted_order_and_stock_verifier(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "external_writes_enabled", True)
    monkeypatch.setenv("OMS_HATUNA_AUTO_WRITES_ENABLED", "true")
    async def run():
        with pytest.raises(HatunaPreflightError, match="Требуется сверка"):
            await HatunaDocumentRunner().apply(**kwargs())
    asyncio.run(run())
