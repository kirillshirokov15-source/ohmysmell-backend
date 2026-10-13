"""Fast manager cards never trade away warehouse/account authorization."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_warehouse_document_locks_use_one_database_read():
    from app.services.warehouse_documents import frozen_plans, plan_key

    one = SimpleNamespace(id=12, allocations=[SimpleNamespace(id=7, order_item_id=11, qty=1)])
    two = SimpleNamespace(id=13, allocations=[SimpleNamespace(id=8, order_item_id=12, qty=2)])
    record = SimpleNamespace(key="warehouse:allocation:7:receipt", status="succeeded")
    released = SimpleNamespace(key=plan_key(two, "release", False), status="succeeded")
    reserved = SimpleNamespace(key=plan_key(two, "reserve", False), status="succeeded")
    session = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(
        all=lambda: [record, reserved, released])))
    result = asyncio.run(frozen_plans(session, [one, two]))
    assert result == {12: True, 13: False}
    assert session.scalars.await_count == 1


def test_source_navigation_read_only_cache_does_not_refetch_each_click(monkeypatch):
    from app.bot import source_navigation
    from app.services.product_service import ProductService

    state = {"time": 100.0}
    monkeypatch.setattr(source_navigation.time, "monotonic", lambda: state["time"])
    monkeypatch.setattr(source_navigation, "_browse_cache", None)
    monkeypatch.setattr(source_navigation, "_browse_loaded_at", 0)
    fetch = AsyncMock(return_value=[{"id": "a", "stocks": []}])
    monkeypatch.setattr(ProductService, "get_catalog_async", fetch)

    async def run():
        a = await source_navigation.catalog_for_navigation()
        state["time"] += 3
        b = await source_navigation.catalog_for_navigation()
        return a, b
    a, b = asyncio.run(run())
    assert a == b
    assert fetch.await_count == 1


def test_readonly_catalog_provider_error_still_fails_closed_without_snapshot(monkeypatch):
    from app.bot import source_navigation
    from app.services.product_service import ProductService
    from app.services.order_desk import DeskError
    monkeypatch.setattr(source_navigation, "_browse_cache", None)
    monkeypatch.setattr(source_navigation, "_browse_loaded_at", 0)
    monkeypatch.setattr(ProductService, "get_catalog_async",
                        AsyncMock(side_effect=RuntimeError("upstream unavailable")))
    with pytest.raises(DeskError, match="МойСклад"):
        asyncio.run(source_navigation.catalog_for_navigation())


def test_manager_cannot_issue_customer_link_before_customer_service_ready(monkeypatch):
    from app.services.order_desk import DeskService, DeskError
    monkeypatch.delenv("CLIENT_LINKS_ENABLED", raising=False)
    with pytest.raises(DeskError, match="ещё не подключён"):
        asyncio.run(DeskService().issue_link(123, actor=111, chat_id=-100222))


def test_site_titles_reuses_supplied_session_without_opening_new_one():
    from app.services.manager_workspace import site_titles_for
    from app.services.manager_workspace import async_session as real_async_session

    order = SimpleNamespace(
        desk_draft_id=4,
        items=[SimpleNamespace(id=3, product_id="p", qty=1, price=1200)])
    source = SimpleNamespace(id=5, product_id="p", qty=1,
                             price=1200, raw_product_text="Site title")
    fake = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(
        all=lambda: [source])))
    titles = asyncio.run(site_titles_for(order, session=fake))
    assert titles == {3: "Site title"}
    assert fake.scalars.await_count == 1
