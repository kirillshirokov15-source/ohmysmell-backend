"""Persist intent before network writes; uncertain results require reconciliation."""
from sqlalchemy import select
from app.database.session import async_session
from app.models.fulfillment import ExternalOperation
from app.services.checkout_service import transaction_lock
from app.integrations.write_guard import require_external_writes


class ExternalOperationConflict(RuntimeError):
    pass


class ExternalOperationStore:
    async def get(self, key):
        async with async_session() as session:
            row = await session.get(ExternalOperation, key)
            return dict(payload=row.payload, status=row.status, result=row.result) if row else None

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
            if operation.status == 'succeeded':
                if result is not None and operation.result != result:
                    raise ExternalOperationConflict('Conflicting external receipt')
                return
            operation.status = "succeeded" if result is not None else "uncertain"
            operation.result = result


class ExternalOperationService:
    def __init__(self, store=None):
        self.store = store or ExternalOperationStore()

    async def execute(self, key, payload, send):
        require_external_writes()
        return await self._execute(key, payload, send)

    async def simulate_document(self, key, payload, adapter):
        from app.integrations.moysklad.documents import FakeDocumentAdapter
        from app.config.settings import settings
        if type(adapter) is not FakeDocumentAdapter or settings.environment == 'production':
            raise ExternalOperationConflict('Simulation requires the local fake adapter')
        return await self._execute('simulation:' + key, payload, adapter.create)

    async def _execute(self, key, payload, send):
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

    async def reconcile_document(self, key, adapter):
        """Read only provider lookup; never retries a missing/ambiguous create."""
        from app.integrations.moysklad.documents import verified_document
        operation = await self.store.get(key)
        if not operation:
            raise ExternalOperationConflict('Operation not found')
        if operation['status'] == 'succeeded':
            return operation['result']
        candidates = await adapter.find(operation['payload'])
        if len(candidates) != 1 or not verified_document(operation['payload'], candidates[0]):
            raise ExternalOperationConflict('Document absent, ambiguous or inconsistent; manual review required')
        await self.store.finish(key, candidates[0])
        return candidates[0]
