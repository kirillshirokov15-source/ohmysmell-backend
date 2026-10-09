from app.integrations.moysklad.client import MoySkladClient
from app.integrations.moysklad.async_gateway import AsyncMoySkladGateway
from app.repositories.order_repository import (
    get_order,
    set_moysklad_order,
)
from app.config.settings import settings
from app.services.external_operation_service import ExternalOperationService


class MoySkladOrderError(Exception):
    pass


class MoySkladOrderService:
    def __init__(self, client: MoySkladClient | None = None, operations=None) -> None:
        self.client = client
        self.operations = operations or ExternalOperationService()

    async def create_from_crm_order(self, order_id: int) -> dict:
        if not settings.external_writes_enabled:
            raise MoySkladOrderError("External writes are disabled")

        order = await get_order(order_id)

        if order is None:
            raise MoySkladOrderError(
                f"CRM-заказ #{order_id} не найден"
            )

        # Заказ в МойСкладе уже был создан
        if getattr(order, "manual_fulfillment", False):
            raise MoySkladOrderError("Заказы Tilda обслуживаются вручную; экспорт отключён")
        if order.moysklad_order_id:
            return {
                "id": order.moysklad_order_id,
                "name": order.moysklad_order_name,
                "already_exists": True,
            }

        if not order.counterparty_id:
            raise MoySkladOrderError(
                "Для заказа не выбран контрагент"
            )

        items = []

        for item in order.items:
            items.append(
                {
                    "id": item.product_id,
                    "name": item.name,
                    "price": item.price,
                    "qty": item.qty,
                }
            )

        description_parts = [
            f"Заказ Oh My Smell #{order.id}",
            f"Клиент: {order.customer_name}",
            f"Телефон: {order.phone}",
        ]

        if order.telegram:
            description_parts.append(
                f"Telegram: {order.telegram}"
            )

        if order.comment:
            description_parts.append(
                f"Комментарий: {order.comment}"
            )

        description = "\n".join(description_parts)

        client = self.client or MoySkladClient()
        arguments = dict(organization_id=settings.moysklad_organization_id,
                         counterparty_id=order.counterparty_id, items=items,
                         description=description, operation_key=f"customerorder:{order.id}")
        async def send(arguments):
            return await AsyncMoySkladGateway(client).create_customer_order(**arguments)
        result = await self.operations.execute(f"customerorder:{order.id}", arguments, send)

        await set_moysklad_order(
            order_id=order.id,
            moysklad_order_id=result["id"],
            moysklad_order_name=result["name"],
        )

        result["already_exists"] = False

        return result
