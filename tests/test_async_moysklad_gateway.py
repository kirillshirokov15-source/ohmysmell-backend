import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.integrations.moysklad.async_gateway import (
    AsyncMoySkladGateway,
    ProductCatalogTTLCache,
)
from app.models.draft_order import ProductMatchStatus
from app.services.counterparty_matching_service import CounterpartyMatchingService
from app.services.product_matching_service import ProductMatchingService


PRODUCTS = [{"id": "p1", "name": "Exact Product", "article": "EXACT-1"}]


def gateway_with_clock(clock):
    client = SimpleNamespace(get_products=Mock(return_value=list(PRODUCTS)))
    cache = ProductCatalogTTLCache(ttl_seconds=60, clock=lambda: clock[0])
    return AsyncMoySkladGateway(client, cache), client


def test_product_fetch_uses_asyncio_to_thread():
    gateway, client = gateway_with_clock([0])

    async def run():
        with patch(
            "app.integrations.moysklad.async_gateway.asyncio.to_thread",
            new_callable=AsyncMock,
            side_effect=lambda function, *args, **kwargs: function(
                *args, **kwargs
            ),
        ) as to_thread:
            assert await gateway.get_products() == PRODUCTS
        to_thread.assert_called_once_with(client.get_products)

    asyncio.run(run())


def test_slow_sync_product_fetch_does_not_block_event_loop():
    events = []

    def slow_fetch():
        time.sleep(0.08)
        events.append("fetch_completed")
        return list(PRODUCTS)

    client = SimpleNamespace(get_products=slow_fetch)
    gateway = AsyncMoySkladGateway(
        client, ProductCatalogTTLCache(ttl_seconds=60)
    )

    async def run():
        fetch = asyncio.create_task(gateway.get_products())
        await asyncio.sleep(0.01)
        events.append("event_loop_progressed")
        await fetch

    asyncio.run(run())
    assert events == ["event_loop_progressed", "fetch_completed"]


def test_catalog_cache_miss_then_hit_within_ttl():
    gateway, client = gateway_with_clock([0])

    async def run():
        first = await gateway.get_products()
        second = await gateway.get_products()
        return first, second

    first, second = asyncio.run(run())
    assert first == second == PRODUCTS
    client.get_products.assert_called_once()


def test_catalog_cache_refetches_after_ttl():
    clock = [0]
    gateway, client = gateway_with_clock(clock)

    async def run():
        await gateway.get_products()
        clock[0] = 61
        await gateway.get_products()

    asyncio.run(run())
    assert client.get_products.call_count == 2


def test_catalog_cache_explicit_invalidate_refetches():
    gateway, client = gateway_with_clock([0])

    async def run():
        await gateway.get_products()
        gateway.invalidate_products()
        await gateway.get_products()

    asyncio.run(run())
    assert client.get_products.call_count == 2


def test_counterparty_search_uses_non_blocking_gateway():
    provider = SimpleNamespace(
        search_counterparties=Mock(return_value=[{"id": "c1", "name": "Customer"}]),
        get_recent_counterparties=Mock(return_value=[]),
    )
    service = CounterpartyMatchingService(provider)

    async def run():
        with patch(
            "app.integrations.moysklad.async_gateway.asyncio.to_thread",
            new_callable=AsyncMock,
            side_effect=lambda function, *args, **kwargs: function(
                *args, **kwargs
            ),
        ) as to_thread:
            result = await service.candidates_async("private query")
        return result, to_thread

    result, to_thread = asyncio.run(run())
    assert result == [
        {"id": "c1", "name": "Customer", "email": None, "phone": None}
    ]
    to_thread.assert_called_once_with(
        provider.search_counterparties, "private query"
    )


def test_async_product_matching_preserves_matching_result():
    provider = SimpleNamespace(get_products=Mock(return_value=list(PRODUCTS)))
    service = ProductMatchingService(provider)

    sync_result = service.match("EXACT-1", 2)
    async_result = asyncio.run(service.match_async("EXACT-1", 2))

    assert sync_result.status == async_result.status == ProductMatchStatus.MATCHED
    assert sync_result.product == async_result.product == PRODUCTS[0]
    assert sync_result.qty == async_result.qty == 2
