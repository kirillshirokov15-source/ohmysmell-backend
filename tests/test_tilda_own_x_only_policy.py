"""Tilda-only warehouse scope. No remote provider, mail, or financial writes."""
import asyncio
from types import SimpleNamespace

import pytest

from app.config.settings import settings
from app.services.order_desk import DeskError
from app.services.order_source_service import (
    OrderSourceService, is_x_warehouse_name, validate_site_source_warehouse,
)
from app.bot.source_navigation import source_warehouses


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize("sources", [
    ["external"],
    ["own", "external"],
    ["partner_x", "external"],
])
def test_no_tilda_allocations_from_external_suppliers(environment, sources, monkeypatch):
    monkeypatch.setattr(settings, "environment", environment)
    class MustNotCallProvider:
        def get_catalog_async(self, **kwargs):
            raise AssertionError("No catalog, Buying or warehouse IO allowed")
    parts = [SimpleNamespace(source_type=source) for source in sources]
    with pytest.raises(DeskError, match="Tilda"):
        asyncio.run(OrderSourceService(products=MustNotCallProvider())._assign(
            None, None, [], parts, actor=123))


@pytest.mark.parametrize("name,expected", [
    ("Склад Х", True), ("Склад X", True),
    (" СКЛАД   Х ", True), ("склад x", True),
    ("Основной склад", False), ("Х поставщик", False),
    ("", False), (None, False), (1, False),
])
def test_x_warehouse_name_normalization(name, expected):
    assert is_x_warehouse_name(name) is expected


@pytest.mark.parametrize("source,name,allowed", [
    ("own", "Основной склад", True),
    ("partner_x", "Склад Х", True),
    ("partner_x", "Склад X", True),
    ("own", "Склад Х", False),
    ("partner_x", "Основной склад", False),
    ("external", "Основной склад", False),
    ("own", None, False),
])
def test_warehouse_type_checks_never_swap_x_and_own(source, name, allowed):
    store = {"id": "uuid", "name": name}
    if allowed:
        assert validate_site_source_warehouse(source, store) is None
    else:
        with pytest.raises(DeskError):
            validate_site_source_warehouse(source, store)


def test_manager_options_have_distinct_own_and_x_warehouses():
    product = {"stocks": [
        {"id": "own-uuid", "name": "Основной склад"},
        {"id": "x-uuid", "name": "Склад Х"},
    ]}
    ids = ("own-uuid", "x-uuid")
    assert [x["id"] for x in source_warehouses(product, ids, "own")] == ["own-uuid"]
    assert [x["id"] for x in source_warehouses(product, ids, "partner_x")] == ["x-uuid"]
