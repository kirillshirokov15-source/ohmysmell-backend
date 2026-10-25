"""Reconcile a previously attempted OWN-stock reservation without creating another.

Never retries a provider POST. A saved journal intent is reconciled to exactly
one full, verified existing MoySklad customerorder before the missing local
OrderEvent is recovered. This module does not notify customers.
"""
import hashlib
import json
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config.settings import settings
from app.database.session import async_session
from app.models.order import Order
from app.models.order_desk import OrderDesk
from app.models.operations import OrderEvent
from app.models.fulfillment import ExternalOperation
from app.services.order_source_service import source_plans
from app.services.order_desk import DeskError, authorized_manager, require_owner
from app.services.warehouse_documents import plan_key
from app.integrations.moysklad.payloads import reservation
from app.integrations.moysklad.documents import MoySkladDocumentAdapter, verified_document


class OwnReservationReconciliationError(DeskError):
    pass


def reserve_intent(order, plan):
    """Pure computation matching the original WarehouseDocumentService intent."""
    if not settings.moysklad_organization_id or not order.counterparty_id:
        raise OwnReservationReconciliationError(
            "Не сопоставлены организация или покупатель МойСклад.")
    active = [a for a in plan.allocations if a.supply_status != "cancelled"]
    if not active or not all(
        a.managed_source and a.source_type == "own" and a.supply_status == "confirmed"
        for a in active
    ):
        raise OwnReservationReconciliationError(
            "Для этого склада нет однозначного собственного источника.")
    items = {item.id: item for item in order.items}
    positions = []
    for a in active:
        item = items.get(a.order_item_id)
        if not item:
            raise OwnReservationReconciliationError("Карточка товара изменилась.")
        positions.append({"id": item.product_id, "qty": a.qty, "price": item.price})
    return {"entity": "customerorder", "document": reservation(
        settings.moysklad_organization_id, order.counterparty_id,
        plan.warehouse_id, positions, plan_key(plan, "reserve", False))}


class OwnReservationReconciler:
    def __init__(self, *, sessions=None, adapter=None, store=None):
        self.sessions = sessions or async_session
        self.adapter = adapter or MoySkladDocumentAdapter()
        if store is None:
            from app.services.external_operation_service import ExternalOperationStore
            store = ExternalOperationStore()
        self.store = store

    async def reconcile(self, order_id, plan_id, actor, chat_id):
        if settings.environment != "production" or not settings.external_writes_enabled:
            raise OwnReservationReconciliationError(
                "Сверка доступна только для рабочего склада в production.")

        async with self.sessions() as session:
            manager = await authorized_manager(session, actor, chat_id)
            order = await session.scalar(select(Order).where(Order.id == order_id)
                .options(selectinload(Order.items)))
            desk = await session.scalar(select(OrderDesk).where(OrderDesk.order_id == order_id))
            if not order or not desk or not order.manual_fulfillment or order.source != "website":
                raise OwnReservationReconciliationError("Заказ Tilda не найден.")
            require_owner(desk, actor)
            if order.fulfillment_status != "assembled" or order.needs_review or order.status == "rejected":
                raise OwnReservationReconciliationError(
                    "Сначала завершите сборку заказа.")
            plan = next((p for p in await source_plans(session, order_id)
                         if p.id == plan_id and p.status == "planned"), None)
            if not plan:
                raise OwnReservationReconciliationError(
                    "План склада больше не соответствует заказу.")
            key = plan_key(plan, "reserve", False)
            record = await session.get(ExternalOperation, key)
            if not record or record.status not in {"inflight", "uncertain", "succeeded"}:
                raise OwnReservationReconciliationError(
                    "Нет операции, которую можно сверить. Новый резерв не создаётся.")
            release = await session.get(ExternalOperation, plan_key(plan, "release", False))
            if release:
                raise OwnReservationReconciliationError(
                    "Заказ имеет операцию снятия резерва; требуется ручная сверка.")
            intent = reserve_intent(order, plan)
            if record.payload != intent:
                raise OwnReservationReconciliationError(
                    "Сохранённые параметры отличаются от заказа. Автоматическая сверка запрещена.")
            revision = order.revision
            existing = await session.scalar(select(OrderEvent).where(
                OrderEvent.order_id == order_id, OrderEvent.idempotency_key == key))
            if existing and record.status == "succeeded":
                return {"status": "already_verified", "id": record.result["id"]}

        # Only GET through the provider adapter: never a POST, PUT or replay.
        # A missing, ambiguous or partial provider record leaves the journal
        # untouched and blocks any further reserve attempt.
        try:
            docs = await self.adapter.find(intent)
        except Exception as error:
            raise OwnReservationReconciliationError(
                "МойСклад недоступен для чтения. Резерв не менялся; повтор запрещён.") from error
        if len(docs) != 1 or not verified_document(intent, docs[0]):
            raise OwnReservationReconciliationError(
                "Не найден один однозначно подтверждённый резерв МойСклад. "
                "Автоматическое повторное создание запрещено; нужна ручная сверка.")
        verified = docs[0]
        if record.status == "succeeded":
            if not record.result or record.result.get("id") != verified["id"]:
                raise OwnReservationReconciliationError(
                    "Журнал и МойСклад указывают на разные документы.")
        else:
            # Persist the verified result before reconstructing the local audit.
            # If audit commit fails, a repeat can safely finish the audit only.
            try:
                await self.store.finish(key, verified)
            except Exception as error:
                raise OwnReservationReconciliationError(
                    "Результат найден, но журнал МойСклад не обновлён.") from error

        async with self.sessions() as session, session.begin():
            manager = await authorized_manager(session, actor, chat_id)
            order = await session.scalar(select(Order).where(Order.id == order_id)
                .options(selectinload(Order.items)).with_for_update())
            desk = await session.scalar(select(OrderDesk).where(OrderDesk.order_id == order_id))
            if not desk or not order:
                raise OwnReservationReconciliationError("Заказ изменился при сверке.")
            require_owner(desk, actor)
            plan = next((p for p in await source_plans(session, order_id)
                         if p.id == plan_id and p.status == "planned"), None)
            if not plan or plan_key(plan, "reserve", False) != key:
                raise OwnReservationReconciliationError("Складской план изменился.")
            latest_record = await session.get(ExternalOperation, key)
            if not latest_record or latest_record.status != "succeeded" or (
                latest_record.result or {}).get("id") != verified["id"]:
                raise OwnReservationReconciliationError(
                    "Запись журнала не соответствует документу МойСклад.")
            if order.revision != revision or reserve_intent(order, plan) != intent:
                raise OwnReservationReconciliationError(
                    "Заказ изменился во время сверки. Обновите карточку.")
            old_event = await session.scalar(select(OrderEvent).where(
                OrderEvent.order_id == order_id, OrderEvent.idempotency_key == key))
            if old_event:
                return {"status": "already_verified", "id": verified["id"]}
            order.revision += 1
            session.add(OrderEvent(
                order_id=order_id, manager_id=manager.id,
                action="warehouse_reserve", idempotency_key=key,
                request_hash=hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest(),
                before={"revision":revision, "receipt_cost_minor": None},
                after={"id":verified["id"], "revision":order.revision,
                       "action":"reserve", "simulated":False}))
            # Internal audit only. Customer notifications belong to confirmed
            # fulfillment-status transitions, not warehouse reconciliation.
        return {"status": "reconciled", "id": verified["id"]}
