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
            if order.manual_fulfillment:
                raise StockAllocationError("Заказы Tilda распределяются и отгружаются вручную")
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
            from app.services.supply_service import ready_supply_quantities
            external = await ready_supply_quantities(session, order.id)
            # Honor explicit Tilda stock claims when the existing automatic
            # planner is used by another channel. Legacy plan semantics stay
            # unchanged; both writers serialize against the same stock keys.
            from app.services.order_source_service import lock_source_stock, claimed_stock
            from app.services.stock_allocation import available_units
            physical_ids = {i.product_id for i in order.items if i.id not in external}
            keys = [(p["id"], s["id"]) for p in catalog if p["id"] in physical_ids for s in p.get("stocks", [])]
            await lock_source_stock(session, keys)
            adjusted = []
            for product in catalog:
                if product["id"] not in physical_ids:
                    adjusted.append(product)
                    continue
                stocks = []
                for stock in product.get("stocks", []):
                    claimed = await claimed_stock(session, product["id"], stock["id"], managed_only=True)
                    stocks.append({**stock, "stock": max(0, available_units(stock.get("stock"), stock.get("reserve")) - claimed), "reserve": 0})
                adjusted.append({**product, "stocks": stocks})
            allocations = allocate([{"id": i.product_id, "qty": i.qty} for i in order.items if i.id not in external],
                                   adjusted, settings.warehouse_ids)
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
