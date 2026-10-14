import asyncio
import logging
from time import monotonic, perf_counter
from typing import Callable

from app.config.settings import settings
from app.integrations.moysklad.client import MoySkladClient
from app.logging_utils import log_event


logger = logging.getLogger(__name__)


class ProductCatalogTTLCache:
    def __init__(
        self,
        ttl_seconds: float = settings.moysklad_catalog_cache_ttl_seconds,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._products: list[dict] | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def get(self) -> list[dict] | None:
        if self._products is None or self.clock() >= self._expires_at:
            return None
        return self._products

    def set(self, products: list[dict]) -> None:
        self._products = products
        self._expires_at = self.clock() + self.ttl_seconds

    def invalidate(self) -> None:
        self._products = None
        self._expires_at = 0.0


catalog_cache = ProductCatalogTTLCache()


class OperationTTLCache:
    """Short-lived read model for chat navigation, not inventory decisions."""
    def __init__(self, ttl_seconds):
        self.ttl_seconds = ttl_seconds
        self._data = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def load(self, loader, *, fresh=False):
        if fresh:
            return await loader()
        if self._data is not None and monotonic() < self._expires_at:
            return self._data
        async with self._lock:
            if self._data is None or monotonic() >= self._expires_at:
                value = await loader()
                self._data = value
                self._expires_at = monotonic() + self.ttl_seconds
            return self._data

    def invalidate(self):
        self._data = None
        self._expires_at = 0.0


stores_cache = OperationTTLCache(60.0)
stock_cache = OperationTTLCache(5.0)


class AsyncMoySkladGateway:
    def __init__(
        self,
        client: MoySkladClient,
        product_cache: ProductCatalogTTLCache = catalog_cache,
    ) -> None:
        self.client = client
        self.product_cache = product_cache

    async def _call(self, operation: str, function, *args, **kwargs):
        started_at = perf_counter()
        log_event(logger, "moysklad_call_started", operation=operation)
        try:
            result = await asyncio.to_thread(function, *args, **kwargs)
        except Exception:
            log_event(
                logger,
                "moysklad_call_completed",
                level=logging.ERROR,
                operation=operation,
                outcome="error",
                duration_ms=round((perf_counter() - started_at) * 1000, 2),
            )
            raise
        else:
            fields = {
                "operation": operation,
                "outcome": "success",
                "duration_ms": round((perf_counter() - started_at) * 1000, 2),
            }
            if isinstance(result, (list, tuple)):
                fields["result_count"] = len(result)
            log_event(logger, "moysklad_call_completed", **fields)
            return result

    async def get_products(self) -> list[dict]:
        products = self.product_cache.get()
        if products is not None:
            log_event(
                logger,
                "moysklad_catalog_cache_hit",
                operation="get_products",
                result_count=len(products),
            )
            return products

        log_event(logger, "moysklad_catalog_cache_miss", operation="get_products")
        async with self.product_cache._lock:
            products = self.product_cache.get()
            if products is None:
                products = await self._call("get_products", self.client.get_products)
                self.product_cache.set(products)
            return products

    def invalidate_products(self) -> None:
        self.product_cache.invalidate()

    async def search_counterparties(self, query: str) -> list[dict]:
        return await self._call(
            "search_counterparties", self.client.search_counterparties, query
        )

    async def get_recent_counterparties(self, limit: int = 10) -> list[dict]:
        return await self._call(
            "get_recent_counterparties",
            self.client.get_recent_counterparties,
            limit,
        )

    async def get_counterparty(self, counterparty_id: str) -> dict:
        return await self._call(
            "get_counterparty", self.client.get_counterparty, counterparty_id
        )

    async def find_counterparties_exact(self, field: str, value: str) -> list[dict]:
        return await self._call(
            "find_counterparties_exact", self.client.find_counterparties_exact,
            field, value,
        )

    async def create_counterparty(self, payload: dict) -> dict:
        from app.integrations.write_guard import require_counterparty_creation
        require_counterparty_creation()
        return await self._call(
            "create_counterparty", self.client.create_counterparty, payload,
        )

    async def get_stores(self, *, fresh=False) -> list[dict]:
        cache = stores_cache if self.product_cache is catalog_cache else OperationTTLCache(60)
        return await cache.load(
            lambda: self._call("get_stores", self.client.get_stores),
            fresh=fresh,
        )

    async def get_stock_by_store(self, *, fresh=False) -> dict:
        cache = stock_cache if self.product_cache is catalog_cache else OperationTTLCache(5)
        return await cache.load(
            lambda: self._call("get_stock_by_store", self.client.get_stock_by_store),
            fresh=fresh,
        )

    async def create_customer_order(self, **kwargs) -> dict:
        from app.integrations.write_guard import require_external_writes
        require_external_writes()
        return await self._call(
            "create_customer_order", self.client.create_customer_order, **kwargs
        )
