from sqlalchemy import select
from app.database.session import async_session
from app.models.fulfillment import DeliveryRequest, Shipment
from app.integrations.delivery import CDEKDeliveryAdapter, YandexDeliveryAdapter, ManualCourierAdapter
from app.integrations.write_guard import require_external_writes
from app.services.external_operation_service import ExternalOperationService


class DeliveryService:
    def __init__(self, adapters=None, operations=None):
        self.adapters = adapters
        self.operations = operations or ExternalOperationService()

    async def prepare(self, draft):
        async with async_session() as session, session.begin():
            shipment = await session.get(Shipment, draft.shipment_id, with_for_update=True)
            if not shipment or shipment.status == "cancelled":
                raise ValueError("Отгрузка недоступна")
            existing = (await session.execute(select(DeliveryRequest).where(
                DeliveryRequest.shipment_id == draft.shipment_id))).scalar_one_or_none()
            details = draft.model_dump(mode="json")
            if existing:
                if existing.details != details:
                    raise ValueError("Для отгрузки уже существует другой план доставки")
                return existing
            record = DeliveryRequest(shipment_id=draft.shipment_id, provider=draft.provider,
                idempotency_key=draft.idempotency_key, details=details, status="draft")
            session.add(record)
            await session.flush()
            return record

    async def submit(self, draft):
        require_external_writes()
        record = await self.prepare(draft)
        adapters = self.adapters or {"cdek": CDEKDeliveryAdapter(), "yandex": YandexDeliveryAdapter(), "manual": ManualCourierAdapter()}
        async def send(payload):
            require_external_writes()
            return await adapters[draft.provider].create(draft)
        result = await self.operations.execute("delivery:" + draft.idempotency_key,
                                               draft.model_dump(mode="json"), send)
        async with async_session() as session, session.begin():
            record = await session.get(DeliveryRequest, record.id, with_for_update=True)
            record.status, record.external_id = "created", result["id"]
        return result
