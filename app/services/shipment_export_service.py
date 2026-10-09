import asyncio
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database.session import async_session
from app.models.fulfillment import Shipment
from app.models.order import Order
from app.config.settings import settings
from app.integrations.write_guard import require_external_writes
from app.integrations.moysklad.client import MoySkladClient
from app.integrations.moysklad.payloads import demand
from app.services.external_operation_service import ExternalOperationService


class ShipmentExportService:
    def __init__(self, client=None, operations=None):
        self.client = client
        self.operations = operations or ExternalOperationService()

    async def export(self, shipment_id):
        require_external_writes()
        async with async_session() as session:
            shipment = (await session.execute(select(Shipment).where(Shipment.id == shipment_id)
                .options(selectinload(Shipment.allocations)))).scalar_one_or_none()
            if not shipment:
                raise ValueError("Отгрузка не найдена")
            if shipment.external_id:
                return {"id": shipment.external_id, "already_exists": True}
            if shipment.status != "planned":
                raise ValueError("Отгрузка требует сверки")
            order = (await session.execute(select(Order).where(Order.id == shipment.order_id)
                .options(selectinload(Order.items)))).scalar_one()
            if not order.moysklad_order_id:
                raise ValueError("Сначала требуется подтверждённый customerorder")
            if order.manual_fulfillment:
                raise ValueError("Заказы Tilda обслуживаются вручную")
            items = {i.id: i for i in order.items}
            payload = demand(settings.moysklad_organization_id, order.counterparty_id,
                shipment.warehouse_id, order.moysklad_order_id,
                [{"id": items[a.order_item_id].product_id, "price": items[a.order_item_id].price,
                  "qty": a.qty} for a in shipment.allocations], f"demand:{shipment_id}")
        client = self.client or MoySkladClient()
        async def send(payload):
            require_external_writes()
            return await asyncio.to_thread(client.create_document, "demand", payload)
        result = await self.operations.execute(f"demand:{shipment_id}", payload, send)
        async with async_session() as session, session.begin():
            shipment = await session.get(Shipment, shipment_id, with_for_update=True)
            shipment.external_id = result["id"]
            shipment.status = "exported"
        return result
