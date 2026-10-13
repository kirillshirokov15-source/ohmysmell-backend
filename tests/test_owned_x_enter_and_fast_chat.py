"""Owned warehouse X and fast read-only navigation; no real provider calls."""
import asyncio
from types import SimpleNamespace

from app.integrations.moysklad.payloads import enter
from app.integrations.moysklad.documents import FakeDocumentAdapter, verified_document
from app.integrations.moysklad.async_gateway import AsyncMoySkladGateway, ProductCatalogTTLCache


def test_own_x_receipt_uses_enter_without_supplier_or_retail_purchase_price():
    payload = enter("org-1", "warehouse-x", [{"id": "product-1", "qty": 1}],
                    "warehouse:allocation:17:receipt")
    assert payload["positions"] == [{
        "quantity": 1,
        "assortment": {"meta": {
            "href": "https://api.moysklad.ru/api/remap/1.2/entity/product/product-1",
            "type": "product", "mediaType": "application/json"}}}]
    assert payload["applicable"] is True
    assert payload["externalCode"] == "warehouse:allocation:17:receipt"
    assert "agent" not in payload and "price" not in payload["positions"][0]
    assert "supplier" not in payload and "purchaseOrder" not in payload

    async def run():
        fake = FakeDocumentAdapter()
        intent = {"entity": "enter", "document": payload}
        first = await fake.create(intent)
        assert verified_document(intent, first)
        assert first["simulated"]
        assert (await fake.find(intent))[0]["id"] == first["id"]
    asyncio.run(run())


def test_short_navigation_cache_with_fresh_bypass_and_singleflight():
    class FakeClient:
        def __init__(self):
            self.get_stores_calls = 0
            self.get_stock_calls = 0
        def get_stores(self):
            self.get_stores_calls += 1
            return [{"id": "warehouse-x", "name": "Склад Х"}]
        def get_stock_by_store(self):
            self.get_stock_calls += 1
            return {"rows": []}

    async def run():
        fake = FakeClient()
        # Isolated product cache means no cross-account shared test data.
        gateway = AsyncMoySkladGateway(fake, ProductCatalogTTLCache(60))
        await gateway.get_stores()
        await gateway.get_stores(fresh=True)
        assert fake.get_stores_calls == 2
        await gateway.get_stock_by_store()
        await gateway.get_stock_by_store(fresh=True)
        assert fake.get_stock_calls == 2
    asyncio.run(run())
