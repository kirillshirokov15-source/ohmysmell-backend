"""Persist intent before network writes; uncertain results require reconciliation."""
from sqlalchemy import select
from app.database.session import async_session
from app.models.fulfillment import ExternalOperation
from app.services.checkout_service import transaction_lock
from app.integrations.write_guard import require_external_writes


class ExternalOperationConflict(RuntimeError):
    pass


class ExternalOperationStore:
    async def claim(self, key, payload):
        async with async_session() as session, session.begin():
            await transaction_lock(session, "external:" + key)
            operation = await session.get(ExternalOperation, key)
            if operation:
                if operation.payload != payload:
                    raise ExternalOperationConflict("Параметры внешней операции изменились")
                if operation.status == "succeeded":
                    return operation.result
                raise ExternalOperationConflict("Результат внешней операции требует сверки; повторная запись заблокирована")
            session.add(ExternalOperation(key=key, payload=payload, status="inflight"))
        return None

    async def finish(self, key, result=None):
        async with async_session() as session, session.begin():
            operation = (await session.execute(select(ExternalOperation).where(
                ExternalOperation.key == key).with_for_update())).scalar_one()
            operation.status = "succeeded" if result is not None else "uncertain"
            operation.result = result


class ExternalOperationService:
    def __init__(self, store=None):
        self.store = store or ExternalOperationStore()

    async def execute(self, key, payload, send):
        require_external_writes()
        result = await self.store.claim(key, payload)
        if result is not None:
            return result
        try:
            result = await send(payload)
            if not isinstance(result, dict) or not result.get("id"):
                raise ExternalOperationConflict("Внешняя система вернула неполный результат")
        except BaseException:
            await self.store.finish(key)
            raise
        await self.store.finish(key, result)
        return result
