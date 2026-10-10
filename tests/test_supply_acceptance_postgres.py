"""Synthetic supply acceptance in the runner's isolated PostgreSQL schema."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from tests.test_postgres_staging import staging_sessions
from app.models.manager import Manager
from app.models.order import Order
from app.models.fulfillment import Shipment
from app.models.supply import Supplier, ProductSupply, SupplierOffer, ProcurementRequest, XSettlement
from app.repositories.order_repository import create_order
from app.services.fulfillment_service import FulfillmentService
from app.services.order_operations import OrderOperations, OrderAction, OperationError
from app.services.supply_service import ProcurementService
from app.services.stock_allocation import StockAllocationError

pytestmark = pytest.mark.staging


@pytest.mark.parametrize("source", ["own", "partner_x", "external"])
def test_source_receipt_and_shipment_revalidation(staging_sessions, source):
    factory, engine = staging_sessions
    async def run():
        pid, actor = uuid4().hex, int(uuid4().hex[:12], 16)
        async with factory() as s, s.begin():
            s.add(Manager(telegram_id=actor, name="SYNTHETIC acceptance", is_active=True))
            supplier = Supplier(name="SYNTHETIC supplier", supplier_type="partner_x" if source == "partner_x" else "external_wholesaler")
            s.add(supplier)
            await s.flush()
            s.add(ProductSupply(product_id=pid, source_type=source,
                supplier_id=supplier.id if source == "partner_x" else None,
                base_cost_minor=10000 if source == "partner_x" else None))
            await s.flush()
            offer = SupplierOffer(product_id=pid, supplier_id=supplier.id,
                purchase_price_minor=10000, currency_code="RUB", availability="confirmed", availability_qty=2)
            s.add(offer)
            await s.flush()
        order = await create_order(dict(customer_name="SYNTHETIC acceptance", phone="", customer_type="wholesale",
            source="email", total=40000, items=[dict(id=pid, name="Synthetic", price=20000, qty=2, sum=40000)]))
        ops = OrderOperations()
        async def act(action, **extra):
            nonlocal order
            order = await ops.act(order.id, actor, OrderAction(action=action, expected_revision=order.revision,
                idempotency_key=uuid4().hex, **extra))
        with pytest.raises(OperationError):
            await act("assembling")
        if source == "external":
            async with factory() as s:
                request = await s.scalar(select(ProcurementRequest).where(ProcurementRequest.order_id == order.id))
            procurement = ProcurementService()
            for revision, action in enumerate(("select", "requested", "confirmed")):
                await procurement.act(request.id, actor, action, revision, offer_id=offer.id if action == "select" else None)
            with pytest.raises(OperationError):
                await act("assembling")
            await procurement.act(request.id, actor, "received", 3)
        else:
            catalog = [{"id": pid, "supply_source": source, "stocks": [{"id": "synthetic-warehouse", "stock": 0, "reserve": 0}]}]
            planner = FulfillmentService(SimpleNamespace(get_catalog_async=AsyncMock(return_value=catalog)))
            with pytest.raises(StockAllocationError):
                await planner.plan(order.id)
            # Simulate a fresh read after physical receipt, never write MoySklad.
            catalog[0]["stocks"][0]["stock"] = 2
            plans = await planner.plan(order.id)
            assert sum(a.qty for p in plans for a in p.allocations) == 2
            assert [p.id for p in await planner.plan(order.id)] == [p.id for p in plans]
        await act("assembling")
        await act("assembled")
        await act("delivery", delivery_method="manual")
        if source != "external":
            async with factory() as s, s.begin():
                (await s.get(Shipment, plans[0].id)).status = "cancelled"
            with pytest.raises(OperationError):
                await act("shipped")
            async with factory() as s, s.begin():
                assert (await s.get(Order, order.id)).fulfillment_status == "assembled"
                (await s.get(Shipment, plans[0].id)).status = "planned"
        await act("shipped")
        assert order.fulfillment_status == "shipped"
        async with factory() as s:
            if source == "partner_x":
                settlement = await s.scalar(select(XSettlement).where(XSettlement.order_id == order.id))
                assert settlement.partner_due_minor == 30000 and settlement.our_margin_minor == 10000
            if source == "external":
                assert await s.scalar(select(func.count()).select_from(Shipment).where(Shipment.order_id == order.id)) == 0
        await engine.dispose()
    asyncio.run(run())
