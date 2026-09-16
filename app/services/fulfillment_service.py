from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database.session import async_session
from app.models.order import Order
from app.models.fulfillment import Shipment, WarehouseAllocation
from app.services.product_service import ProductService
from app.services.stock_allocation import allocate, StockAllocationError
from app.config.settings import settings


class FulfillmentService:
    def __init__(self, products=None):
        self.products = products or ProductService()

    async def plan(self, order_id: int, expected_revision=None):
        # No price decision here; stock is never taken from the frontend/cache.
        catalog = await self.products.get_catalog_async()
        async with async_session() as session, session.begin():
            order = (await session.execute(select(Order).where(Order.id == order_id)
                     .options(selectinload(Order.items)).with_for_update())).scalar_one_or_none()
            if not order:
                raise StockAllocationError("Заказ не найден")
            if expected_revision is not None and order.revision != expected_revision:
                raise StockAllocationError("Карточка устарела; обновите заказ")
            if order.fulfillment_status != "new" or order.needs_review:
                raise StockAllocationError("Распределение недоступно в этом состоянии")
            existing = list((await session.execute(select(Shipment)
                .where(Shipment.order_id == order_id)
                .options(selectinload(Shipment.allocations)))).scalars())
            if existing:
                return existing
            if order.status != "new":
                raise StockAllocationError("Планирование недоступно в этом статусе")
            allocations = allocate([{"id": i.product_id, "qty": i.qty} for i in order.items],
                                   catalog, settings.warehouse_ids)
            # Repeated product lines are distributed without changing order items.
            remaining = {i.id: i.qty for i in order.items}
            shipments = {}
            for allocation in allocations:
                shipment = shipments.setdefault(allocation.warehouse_id,
                    Shipment(order_id=order.id, warehouse_id=allocation.warehouse_id))
                qty = allocation.qty
                for item in order.items:
                    if item.product_id != allocation.product_id:
                        continue
                    take = min(qty, remaining[item.id])
                    if take:
                        shipment.allocations.append(WarehouseAllocation(order_item_id=item.id, qty=take))
                        remaining[item.id] -= take
                        qty -= take
            session.add_all(shipments.values())
            await session.flush()
            return list(shipments.values())
