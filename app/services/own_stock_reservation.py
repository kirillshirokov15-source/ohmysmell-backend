"""Safe post-assembly reservation for verified OWN-only Tilda orders.

Tilda source assignment is local; only the completed physical-picking step
may reserve real MoySklad stock. Previously assembled orders can explicitly
recover this step through the manager's warehouse screen. All writes use the
existing durable ExternalOperation ledger and exact allocation generation.
"""
from sqlalchemy import select
from app.config.settings import settings
from app.database.session import async_session
from app.models.order import Order
from app.models.fulfillment import ExternalOperation
from app.models.operations import OrderEvent
from app.services.order_source_service import source_plans
from app.services.order_desk import DeskError
from app.services.warehouse_documents import WarehouseDocumentService, plan_key


async def reserve_assembled_own_order(order_id: int, actor: int, chat_id: int) -> dict:
    if settings.environment != "production" or not settings.external_writes_enabled:
        raise DeskError("Реальное резервирование доступно только в production.")
    from app.services.hatuna_post_pick import post_pick_enabled_for_order
    if not post_pick_enabled_for_order(order_id):
        raise DeskError("Для заказа не включён сценарий сборки перед резервом.")

    async with async_session() as session:
        order = await session.get(Order, order_id)
        if not order or not order.manual_fulfillment or order.source != "website":
            raise DeskError("Резерв доступен только для заказов Tilda.")
        if order.status == "rejected" or order.needs_review or order.fulfillment_status != "assembled":
            raise DeskError("Сначала подтвердите сборку и завершите проверку заказа.")
        if not order.counterparty_id:
            raise DeskError("Не сопоставлен покупатель МойСклад. Сначала сверьте контрагента.")
        plans = await source_plans(session, order_id)
        if not plans:
            raise DeskError("Не назначен собственный склад.")
        pending = []
        for plan in plans:
            active = [a for a in plan.allocations if a.supply_status != "cancelled"]
            if not active or any(
                not a.managed_source or a.source_type != "own"
                or a.supply_status != "confirmed" for a in active
            ):
                # X orders are reserved by the separate X worker only after enter.
                raise DeskError("Для смешанных и внешних источников используется отдельный сценарий.")
            if plan.status == "exported":
                continue
            if plan.status != "planned":
                raise DeskError("Складской план недоступен для резервирования.")
            record = await session.get(ExternalOperation, plan_key(plan, "reserve", False))
            if record is None:
                pending.append(plan.id)
            elif record.status != "succeeded":
                raise DeskError("Результат резерва неизвестен. Откройте «Сверить резерв МойСклад»; "
                                "создание нового документа запрещено.")
            else:
                audited = await session.scalar(select(OrderEvent.id).where(
                    OrderEvent.order_id == order_id,
                    OrderEvent.idempotency_key == plan_key(plan, "reserve", False)))
                if audited is None:
                    raise DeskError("Документ записан в журнал, но не связан с заказом. "
                                    "Откройте «Сверить резерв МойСклад».")
            release = await session.get(ExternalOperation, plan_key(plan, "release", False))
            if release:
                raise DeskError("Резерв уже снимался: необходима сверка документов.")
    created = 0
    warehouse = WarehouseDocumentService()
    for plan_id in pending:
        async with async_session() as session:
            latest = await session.get(Order, order_id)
            revision = latest.revision
        await warehouse.act(order_id, actor, chat_id, revision, "reserve", plan_id)
        created += 1
    return {"reserved": True, "created": created}


async def dispatch_assembled_own_order(order_id: int, actor: int, chat_id: int) -> dict:
    """Explicit manager dispatch: create real demand before marking shipped.

    The click on 'Отгружен' is the dispatch confirmation. This operation
    never executes during assembly and never notifies clients on its own.
    """
    if settings.environment != "production" or not settings.external_writes_enabled:
        raise DeskError("Реальная отгрузка доступна только в production.")
    from app.services.hatuna_post_pick import post_pick_enabled_for_order
    if not post_pick_enabled_for_order(order_id):
        raise DeskError("Для заказа не включён сценарий складских документов.")

    async with async_session() as session:
        order = await session.get(Order, order_id)
        if not order or not order.manual_fulfillment or order.source != "website":
            raise DeskError("Только подтверждённые заказы Tilda.")
        if order.needs_review or order.fulfillment_status != "assembled":
            raise DeskError("Отгрузка разрешена только после подтверждения сборки.")
        if order.delivery_method == "unselected":
            raise DeskError("Сначала выберите способ доставки.")
        plans = await source_plans(session, order_id)
        if not plans:
            raise DeskError("Не выбраны склады заказа.")
        pending = []
        for plan in plans:
            active = [a for a in plan.allocations if a.supply_status != "cancelled"]
            if not active or any(
                not a.managed_source or a.source_type != "own"
                or a.supply_status != "confirmed" for a in active
            ):
                raise DeskError("Отгрузка смешанного заказа требует отдельной сверки X/закупок.")
            if plan.status == "exported":
                continue
            if plan.status != "planned":
                raise DeskError("План отгрузки недоступен.")
            reserve = await session.get(ExternalOperation, plan_key(plan, "reserve", False))
            release = await session.get(ExternalOperation, plan_key(plan, "release", False))
            if not reserve or reserve.status != "succeeded" or release:
                raise DeskError("Внешний резерв МойСклад отсутствует или требует сверки.")
            audited = await session.scalar(select(OrderEvent.id).where(
                OrderEvent.order_id == order_id,
                OrderEvent.idempotency_key == plan_key(plan, "reserve", False)))
            if audited is None:
                raise DeskError("Резерв ещё не связан с заказом. Сначала нажмите «Сверить резерв МойСклад».")
            ship = await session.get(ExternalOperation, plan_key(plan, "ship", False))
            if ship is not None and ship.status != "succeeded":
                raise DeskError("Неопределённый результат отгрузки: повтор запрещён до сверки.")
            pending.append(plan.id)

    warehouse = WarehouseDocumentService()
    created = 0
    for plan_id in pending:
        async with async_session() as session:
            revision = (await session.get(Order, order_id)).revision
        await warehouse.act(order_id, actor, chat_id, revision, "ship", plan_id)
        created += 1
    return {"documents_created": created}
