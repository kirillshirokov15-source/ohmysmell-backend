import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.config.settings import settings
from app.integrations.moysklad.client import MoySkladClient
from app.services.draft_order_service import DraftOrderService
from app.services.moysklad_order_service import (
    MoySkladOrderError,
    MoySkladOrderService,
)
from app.services.order_lifecycle import OrderStatus


def test_external_writes_default_to_false():
    assert settings.external_writes_enabled is False


def test_moysklad_write_is_blocked_before_local_or_external_work():
    client = Mock()
    with (
        patch.object(settings, "external_writes_enabled", False),
        patch(
            "app.services.moysklad_order_service.get_order",
            new=AsyncMock(),
        ) as get_order,
    ):
        with pytest.raises(
            MoySkladOrderError,
            match="^External writes are disabled$",
        ):
            asyncio.run(
                MoySkladOrderService(client).create_from_crm_order(1)
            )

    get_order.assert_not_awaited()
    client.create_customer_order.assert_not_called()


def test_read_only_moysklad_operation_is_unaffected():
    client = MoySkladClient.__new__(MoySkladClient)
    client.base_url = "https://example.invalid/api"
    client.headers = {}
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"rows": [{"id": "product-1"}]}

    with (
        patch.object(settings, "external_writes_enabled", False),
        patch(
            "app.integrations.moysklad.client.requests.get",
            return_value=response,
        ) as get,
    ):
        assert client.get_products() == [{"id": "product-1"}]

    get.assert_called_once()


def test_enabling_external_writes_reaches_mocked_integration():
    client = Mock()
    client.create_customer_order.return_value = {
        "id": "ms-order-1",
        "name": "MS-1",
    }
    order = SimpleNamespace(
        id=1,
        moysklad_order_id=None,
        moysklad_order_name=None,
        counterparty_id="counterparty-1",
        customer_name="Buyer",
        phone="",
        telegram=None,
        comment=None,
        items=[
            SimpleNamespace(
                product_id="product-1",
                name="Product",
                price=9999,
                qty=2,
            )
        ],
    )

    with (
        patch.object(settings, "external_writes_enabled", True),
        patch(
            "app.services.moysklad_order_service.get_order",
            new=AsyncMock(return_value=order),
        ),
        patch(
            "app.services.moysklad_order_service.set_moysklad_order",
            new=AsyncMock(),
        ) as set_moysklad_order,
    ):
        result = asyncio.run(
            MoySkladOrderService(client).create_from_crm_order(1)
        )

    assert result["already_exists"] is False
    client.create_customer_order.assert_called_once()
    set_moysklad_order.assert_awaited_once_with(
        order_id=1,
        moysklad_order_id="ms-order-1",
        moysklad_order_name="MS-1",
    )


def test_finalize_draft_remains_local_only():
    draft = SimpleNamespace(id=5, finalized_order_id=None)
    local_order = SimpleNamespace(id=10, status=OrderStatus.NEW)
    repository = SimpleNamespace(
        get=AsyncMock(return_value=draft),
        finalize=AsyncMock(return_value=local_order),
    )
    service = DraftOrderService(repository=repository)
    service.review = AsyncMock(
        return_value=SimpleNamespace(status=OrderStatus.READY)
    )

    with patch.object(settings, "external_writes_enabled", False):
        result = asyncio.run(service.finalize(5))

    assert result is local_order
    repository.finalize.assert_awaited_once_with(5)
