"""Authenticated, versioned local operations. Never calls an external provider."""
import hashlib
import json
import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database.session import async_session
from app.models.manager import Manager
from app.models.order import Order
from app.models.operations import OrderEvent
from app.models.fulfillment import Shipment
from app.logging_utils import log_event

logger = logging.getLogger(__name__)
FULFILLMENT = {"new": "assembling", "assembling": "assembled", "assembled": "shipped"}


class OperationError(ValueError):
    pass


class ManagerDenied(OperationError):
    pass


class OrderAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["assembling", "assembled", "shipped", "paid", "review", "resolve", "cancel", "delivery"]
    expected_revision: int = Field(strict=True, ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=1000)
    delivery_method: Literal["pickup", "manual", "cdek", "yandex"] | None = None
    delivery_status: Literal["pending", "ready", "dispatched", "delivered", "cancelled"] | None = None
    delivery_reference: str | None = Field(default=None, max_length=255)


def snapshot(order):
    return {key: getattr(order, key) for key in ("revision", "fulfillment_status", "payment_status",
        "needs_review", "delivery_method", "delivery_status")}


def apply_action(order, request, manager_id, now):
    if order.revision != request.expected_revision:
        raise OperationError("Карточка устарела. Нажмите «Обновить».")
    action = request.action
    if order.fulfillment_status == "cancelled" or order.status == "rejected":
        raise OperationError("Заказ отменён. Изменение недоступно.")
    if action in FULFILLMENT.values():
        if order.needs_review:
            raise OperationError("Сначала завершите проверку заказа.")
        if FULFILLMENT.get(order.fulfillment_status) != action:
            raise OperationError("Недопустимый переход сборки. Обновите карточку.")
        order.fulfillment_status = action
        setattr(order, {"assembling": "assembling_at", "assembled": "assembled_at", "shipped": "shipped_at"}[action], now)
        if action == "shipped":
            if order.delivery_method == "unselected":
                raise OperationError("Сначала выберите способ доставки.")
            order.delivery_status = "dispatched"
    elif action == "paid":
        if order.payment_status != "unpaid":
            raise OperationError("Оплата уже отмечена. Обновите карточку.")
        order.payment_status, order.paid_at = "paid", now
        order.paid_by_manager_id, order.payment_note = manager_id, request.note
    elif action in {"review", "resolve"}:
        target = action == "review"
        if order.needs_review == target:
            raise OperationError("Проверка уже в указанном состоянии.")
        order.needs_review = target
    elif action == "cancel":
        if order.fulfillment_status == "shipped" or order.payment_status == "paid":
            raise OperationError("Оплаченный или отгруженный заказ требует проверки, а не отмены.")
        order.fulfillment_status, order.delivery_status = "cancelled", "cancelled"
    elif action == "delivery":
        if request.delivery_method:
            if order.fulfillment_status == "shipped":
                raise OperationError("Способ доставки после отгрузки не меняется.")
            order.delivery_method = request.delivery_method
        if request.delivery_status:
            transitions = {"pending": {"ready"}, "ready": set(), "dispatched": {"delivered"}, "delivered": set(), "cancelled": set()}
            if request.delivery_status not in transitions[order.delivery_status]:
                raise OperationError("Недопустимый переход доставки. Отгрузка отмечается отдельно.")
            if order.delivery_method == "unselected":
                raise OperationError("Выберите способ доставки.")
            order.delivery_status = request.delivery_status
        if request.delivery_reference is not None:
            order.delivery_reference = request.delivery_reference
        if not any((request.delivery_method, request.delivery_status, request.delivery_reference)):
            raise OperationError("Укажите способ, статус или номер доставки.")
    order.revision += 1
    order.status_changed_at, order.status_changed_by_manager_id = now, manager_id


class OrderOperations:
    async def act(self, order_id: int, telegram_id: int, request: OrderAction):
        started = perf_counter()
        digest = hashlib.sha256(request.model_dump_json(exclude={"idempotency_key"}).encode()).hexdigest()
        async with async_session() as session, session.begin():
            manager = (await session.execute(select(Manager).where(Manager.telegram_id == telegram_id,
                Manager.is_active.is_(True)).with_for_update(read=True))).scalar_one_or_none()
            if not manager:
                raise ManagerDenied("Нет доступа менеджера.")
            order = (await session.execute(select(Order).where(Order.id == order_id)
                .options(selectinload(Order.items)).with_for_update())).scalar_one_or_none()
            if not order:
                raise OperationError("Заказ не найден.")
            if order.manual_fulfillment:
                from app.models.order_desk import OrderDesk
                from app.services.order_desk import authorized_manager, manager_chat, require_owner, DeskError
                try:
                    await authorized_manager(session, telegram_id, manager_chat())
                    desk = await session.scalar(select(OrderDesk).where(OrderDesk.order_id == order_id))
                    if not desk:
                        raise DeskError("Нет привязанной заявки.")
                    require_owner(desk, telegram_id)
                except DeskError as error:
                    raise ManagerDenied(str(error)) from None
            previous = (await session.execute(select(OrderEvent).where(OrderEvent.order_id == order_id,
                OrderEvent.idempotency_key == request.idempotency_key))).scalar_one_or_none()
            if previous:
                if previous.request_hash != digest or previous.manager_id != manager.id:
                    raise OperationError("Ключ действия уже использован.")
                return order
            before = snapshot(order)
            if request.action == "resolve":
                from app.models.supply import XSettlement
                financial_review = await session.scalar(select(XSettlement.id).where(
                    XSettlement.order_id == order_id, XSettlement.status == "requires_financial_review"))
                if financial_review:
                    raise OperationError("Продажа ниже стоимости X требует отдельного финансового решения; обычная проверка не снимает блокировку.")
            if request.action == "assembling" and not order.manual_fulfillment:
                plans = list((await session.execute(select(Shipment).where(Shipment.order_id == order_id)
                    .options(selectinload(Shipment.allocations)))).scalars())
                quantities = {}
                for plan in plans:
                    if plan.status == "cancelled":
                        continue
                    for allocation in plan.allocations:
                        quantities[allocation.order_item_id] = quantities.get(allocation.order_item_id, 0) + allocation.qty
                from app.services.supply_service import ready_supply_quantities, SupplyError
                try:
                    quantities.update(await ready_supply_quantities(session, order_id))
                except SupplyError as error:
                    raise OperationError(str(error)) from error
                if quantities != {item.id: item.qty for item in order.items}:
                    raise OperationError("Сначала распределите все товары по складам.")
            apply_action(order, request, manager.id, datetime.now(timezone.utc))
            if request.action == "cancel":
                from app.models.supply import ProcurementRequest, SupplyEvent
                procurements = list((await session.scalars(select(ProcurementRequest).where(
                    ProcurementRequest.order_id == order_id,
                    ProcurementRequest.status.in_(["needed", "requested", "confirmed"]))
                    .with_for_update())).all())
                for procurement in procurements:
                    session.add(SupplyEvent(procurement_id=procurement.id, manager_id=manager.id,
                        action="cancelled", details={"reason": "order_cancelled", "revision": procurement.revision}))
                    procurement.status = "cancelled"
                    procurement.revision += 1
            session.add(OrderEvent(order_id=order.id, manager_id=manager.id, action=request.action,
                idempotency_key=request.idempotency_key, request_hash=digest, before=before, after=snapshot(order)))
            if order.manual_fulfillment:
                from app.services.order_desk import status_notification
                await status_notification(session, order, telegram_id)
            await session.flush()
        event = {"paid": "payment_marked", "delivery": "delivery_status_changed"}.get(request.action, "fulfillment_status_changed")
        log_event(logger, event, order_id=order.id, manager_id=manager.id,
            action=request.action, duration_ms=round((perf_counter()-started)*1000, 2), result="success")
        return order

    async def events(self, order_id):
        async with async_session() as session:
            return list((await session.execute(select(OrderEvent).where(OrderEvent.order_id == order_id)
                .order_by(OrderEvent.id.desc()).limit(100))).scalars())
