"""SQLite end-to-end post-pick flow: Tilda -> own/X -> Hatuna -> two shipments.

No external HTTP and no real Telegram messages or provider documents.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config.settings import settings
from app.integrations.moysklad.documents import FakeDocumentAdapter
from app.models.order import Order, OrderItem
from app.models.fulfillment import Shipment, ExternalOperation
from app.schemas.order_sources import SourceAction
from app.services.order_operations import OrderOperations, OrderAction
from app.services.hatuna_fulfillment import HatunaFulfillment
from app.services.hatuna_post_pick import require_complete_picking_plan
from app.services.order_source_service import OrderSourceService
from tests.test_order_sources import source_db, seed_sources
from tests.test_tilda_sku_review import database, catalog, tilda_config


def test_complete_mixed_owned_and_hatuna_order_after_picker_confirmation(source_db, monkeypatch):
    from app.services import external_operation_service as ledger
    from app.services import hatuna_fulfillment as fulfillment_module
    from app.services.product_service import ProductService

    monkeypatch.setenv("OMS_HATUNA_POST_PICK_ENABLED", "true")
    monkeypatch.setattr(settings, "moysklad_organization_id", str(uuid4()))
    monkeypatch.setattr(ledger, "async_session", source_db)
    monkeypatch.setattr(ledger, "transaction_lock", AsyncMock())
    monkeypatch.setattr(fulfillment_module, "async_session", source_db)

    own_store, x_store, product_id, customer_id, supplier_id = [
        str(uuid4()) for _ in range(5)]
    stock = [
        {"id": own_store, "name": "Основной склад", "stock": 50, "reserve": 0},
        {"id": x_store, "name": "Склад Х", "stock": 0, "reserve": 0},
    ]
    products = SimpleNamespace(get_catalog_async=AsyncMock(return_value=[
        {"id": product_id, "name": "Site name",
         "stocks": stock, "archived": False}
    ]))
    monkeypatch.setattr(ProductService, "get_catalog_async",
        AsyncMock(return_value=products.get_catalog_async.return_value))
    monkeypatch.setenv("OMS_HATUNA_COUNTERPARTY_ID", supplier_id)
    adapter = FakeDocumentAdapter()

    async def run():
        order, service, _ = await seed_sources(source_db)
        order_id = order.id
        item_id = order.items[0].id
        # Allocate with the current source-service contract before assigning
        # real-format MoySklad UUIDs to the synthetic sample.
        source_rows = [
            dict(item_id=item_id, qty=2, source_type="own", warehouse_id="wh1"),
            dict(item_id=item_id, qty=6, source_type="partner_x", warehouse_id="wh2"),
        ]
        await service.act(order_id, 111, -100123456,
            SourceAction(action="assign", parts=source_rows,
                         expected_revision=0, idempotency_key="mixed-sources"))
        async with source_db() as session, session.begin():
            obj = await session.get(Order, order_id)
            obj.counterparty_id = customer_id
            item = await session.get(OrderItem, item_id)
            item.product_id = product_id
            plans = (await session.scalars(
                select(Shipment).where(Shipment.order_id == order_id))).all()
            assert len(plans) == 2
            for plan in plans:
                plan.warehouse_id = own_store if plan.warehouse_id == "wh1" else x_store
        with pytest.raises(Exception):
            # Physical picking has not occurred; provider must remain untouched.
            await HatunaFulfillment(adapter=adapter, catalog=products,
                sessions=source_db).run_order(order_id)
        assert adapter.calls == 0
        await OrderOperations().act(order_id, 111,
            OrderAction(action="assembling", expected_revision=1,
                        idempotency_key="picker-started"))
        await OrderOperations().act(order_id, 111,
            OrderAction(action="delivery", delivery_method="manual",
                        expected_revision=2, idempotency_key="delivery-selected"))
        await OrderOperations().act(order_id, 111,
            OrderAction(action="assembled", expected_revision=3,
                        idempotency_key="picker-confirmed"))
        result = await HatunaFulfillment(adapter=adapter, catalog=products,
            sessions=source_db).run_order(order_id)
        assert result == {
            "order_id": order_id, "shipped": True, "warehouses": 2,
            "hatuna_receipts": 1, "simulated": True}
        assert adapter.calls == 6
        async with source_db() as session:
            final = await session.scalar(select(Order).where(Order.id == order_id).options(selectinload(Order.items)))
            assert final.fulfillment_status == "shipped"
            assert final.total == 8 * 168_000
            assert final.items[0].price == 168_000
            plans = (await session.scalars(
                select(Shipment).where(Shipment.order_id == order_id))).all()
            assert len(plans) == 2 and all(p.status == "exported" and p.external_id for p in plans)
            operations = (await session.scalars(select(ExternalOperation))).all()
            entities = [op.payload["entity"] for op in operations]
            assert sorted(entities) == sorted([
                "purchaseorder", "supply", "customerorder",
                "customerorder", "demand", "demand"
            ])
            po = next(op.payload["document"] for op in operations
                      if op.payload["entity"] == "purchaseorder")
            assert po["positions"][0]["price"] == 68_000
            assert po["positions"][0]["quantity"] == 6
    asyncio.run(run())
