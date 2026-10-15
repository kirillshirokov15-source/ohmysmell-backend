"""Post-pick Tilda -> ONE Hatuna stock entry -> per-warehouse reserve/shipment.

Only committed 'assembled' orders, full OWN/X allocations, a verified MoySklad
catalog, and a manager-selected delivery may reach the provider. All external
operations are durable and idempotent. Production is opt-in and fail-closed.
"""
import os
from collections import defaultdict
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config.settings import settings
from app.database.session import async_session
from app.models.fulfillment import Shipment, WarehouseAllocation, ExternalOperation
from app.models.order import Order
from app.models.order_desk import OrderDesk
from app.models.operations import OrderEvent
from app.models.manager import Manager
from app.services.hatuna_accounting import (
    HatunaItem, HatunaPreflightError, hatuna_operation_keys,
    provisional_cost, _uuid,
)
from app.services.hatuna_document_runner import HatunaDocumentRunner
from app.services.order_source_service import source_plans
from app.services.hatuna_post_pick import require_complete_picking_plan
from app.services.warehouse_documents import WarehouseDocumentService, plan_key
from app.integrations.moysklad.documents import FakeDocumentAdapter
from app.services.product_service import ProductService


def enabled(order_id):
    from app.services.hatuna_post_pick import post_pick_enabled_for_order
    return post_pick_enabled_for_order(order_id)


class HatunaFulfillment:
    def __init__(self, *, adapter=None, catalog=None, sessions=None):
        self.adapter = adapter
        self.catalog = catalog or ProductService()
        self.sessions = sessions or async_session

    async def snapshot(self, order_id):
        """Fresh DB snapshot; no untrusted client/browser IDs or prices."""
        if not enabled(order_id):
            raise HatunaPreflightError("Post-pick scenario is disabled for this order")
        async with self.sessions() as session:
            order = await session.scalar(select(Order).where(Order.id == order_id)
                                         .options(selectinload(Order.items)))
            desk = await session.scalar(select(OrderDesk).where(OrderDesk.order_id == order_id))
            if not order or not desk or not desk.actor_telegram_id:
                raise HatunaPreflightError("Заказ сайта или его ответственный отсутствует")
            if not order.manual_fulfillment or order.source != "website":
                raise HatunaPreflightError("Только клиентские заказы Tilda")
            if (order.status == "rejected" or order.needs_review
                    or order.fulfillment_status != "assembled"):
                raise HatunaPreflightError("Сначала сборщик должен подтвердить сборку заказа")
            if order.delivery_method == "unselected":
                raise HatunaPreflightError("Сначала выберите способ доставки")
            if "test oms" in (order.customer_name or "").casefold() and settings.environment == "production":
                raise HatunaPreflightError("Реальные документы по тестовому заказу запрещены")
            plans = await source_plans(session, order_id)
            quantities = defaultdict(int)
            x_plans = []
            for plan in plans:
                if plan.status not in {"planned", "exported"}:
                    raise HatunaPreflightError("Недоступный складской план")
                x_allocations = []
                for a in plan.allocations:
                    if a.supply_status == "cancelled":
                        continue
                    if not a.managed_source or a.source_type not in {"own", "partner_x"}:
                        raise HatunaPreflightError("Для Tilda разрешены только свой склад и склад Х")
                    quantities[a.order_item_id] += a.qty
                    if a.source_type == "partner_x":
                        x_allocations.append(a)
                if x_allocations:
                    if any(a.supply_status != "cancelled" and a.source_type != "partner_x"
                           for a in plan.allocations):
                        raise HatunaPreflightError("Склад Х нельзя смешивать с основным в одном плане")
                    x_plans.append(plan)
            if dict(quantities) != {i.id: i.qty for i in order.items}:
                raise HatunaPreflightError("Неполное распределение или превышение количества")
            items = {i.id: i for i in order.items}
            actor = desk.actor_telegram_id
            from app.services.order_desk import manager_chat
            return order, plans, x_plans, items, actor, manager_chat()

    async def preflight(self, order_id, *, for_x=None):
        order, plans, x_plans, items, actor, chat = await self.snapshot(order_id)
        try:
            catalog = {p["id"]: p for p in await self.catalog.get_catalog_async(
                include_supply=False, fresh_stock=True)}
        except Exception as error:
            raise HatunaPreflightError("Каталог и актуальные остатки МойСклад недоступны") from error
        for plan in plans:
            is_x = plan in x_plans
            for a in plan.allocations:
                if a.supply_status == "cancelled":
                    continue
                item = items[a.order_item_id]
                _uuid(item.product_id, "Идентификатор товара")
                product = catalog.get(item.product_id)
                if not product or product.get("archived"):
                    raise HatunaPreflightError(
                        f"Товар {item.id} отсутствует в МойСклад; нужна проверенная карточка без дублей")
                store = next((s for s in product.get("stocks", [])
                              if s["id"] == plan.warehouse_id), None)
                if not store:
                    raise HatunaPreflightError("Склад отсутствует в актуальном каталоге")
                name = "".join((store.get("name") or "").casefold().split()).replace("x", "х")
                if (name == "складх") != is_x:
                    raise HatunaPreflightError("План не соответствует физическому складу")
                if is_x:
                    provisional_cost(item.price)
                    # After a successful *previous* receipt, the inventory
                    # naturally exists; repeat verification uses the journal.
                    receipt_key = hatuna_operation_keys(order.id, plan.id)["receipt"]
                    prefix = "simulation:" if self.adapter is not None and type(self.adapter) is FakeDocumentAdapter else ""
                    async with self.sessions() as session:
                        op = await session.get(ExternalOperation, prefix + receipt_key)
                    if (not op or op.status != "succeeded") and (
                        (store.get("stock") or 0) > 0 or (store.get("reserve") or 0) > 0
                    ):
                        raise HatunaPreflightError(
                            "На складе Х уже есть учтённый остаток. "
                            "Нельзя повторно создавать закупку и приёмку без сверки.")
                if settings.warehouse_ids and plan.warehouse_id not in settings.warehouse_ids:
                    raise HatunaPreflightError("Склад не входит в разрешённые склады МойСклад")
        if for_x is not None and all(p.id != for_x for p in x_plans):
            raise HatunaPreflightError("План Х изменился")
        return order, plans, x_plans, items, actor, chat

    async def confirm_x_receipt(self, order_id, plan_id, receipt_id, *, simulated):
        """After the supplier receipt is journaled, project it onto X allocations."""
        key = ("simulation:" if simulated else "") + hatuna_operation_keys(order_id, plan_id)["receipt"]
        async with self.sessions() as session, session.begin():
            order = await session.scalar(select(Order).where(Order.id == order_id)
                                         .options(selectinload(Order.items)).with_for_update())
            op = await session.get(ExternalOperation, key)
            if not op or op.status != "succeeded" or op.result["id"] != receipt_id:
                raise HatunaPreflightError("Приёмка Хатуны не подтверждена журналом документов")
            plan = await session.scalar(select(Shipment).where(Shipment.id == plan_id,
                Shipment.order_id == order_id).options(selectinload(Shipment.allocations)))
            if not order or order.fulfillment_status != "assembled" or not plan:
                raise HatunaPreflightError("Заказ или складской план изменён")
            active = [a for a in plan.allocations if a.supply_status != "cancelled"]
            if not active or any(a.source_type != "partner_x" for a in active):
                raise HatunaPreflightError("План склада Х имеет неподтверждённые источники")
            if all(a.receipt_reference == receipt_id and a.supply_status == "confirmed" for a in active):
                return
            if any(a.receipt_reference or a.supply_status not in {"awaiting_receipt", "received"}
                   for a in active):
                raise HatunaPreflightError("Статусы X изменились; повторное оприходование запрещено")
            for a in active:
                a.supply_status = "confirmed"
                a.receipt_reference = receipt_id
                a.receipt_simulated = simulated
            order.revision += 1
            session.add(OrderEvent(order_id=order_id, manager_id=order.status_changed_by_manager_id,
                action="hatuna_receipt_linked", idempotency_key=key + ":crm",
                request_hash=op.payload["document"]["syncId"], before={"plan":plan_id},
                after={"receipt_id":receipt_id, "revision":order.revision,
                       "provisional_cost":True, "simulated":simulated}))

    async def run_order(self, order_id):
        fake = type(self.adapter) is FakeDocumentAdapter
        if settings.environment == "production" and fake:
            raise HatunaPreflightError("В production симуляция запрещена")
        order, plans, x_plans, items, actor, chat = await self.preflight(order_id)
        # No Hatuna counterparty is required: the internal `enter` has no agent.
        for plan in x_plans:
            aggregated = defaultdict(int)
            for a in plan.allocations:
                if a.supply_status != "cancelled":
                    aggregated[a.order_item_id] += a.qty
            hatuna_items = [HatunaItem(items[id].product_id, qty, items[id].price, id)
                            for id, qty in sorted(aggregated.items())]
            # One priced accounting entry only; Hatuna is a description label.
            async def verify_again(_):
                await self.preflight(order_id, for_x=plan.id)
            runner = HatunaDocumentRunner(adapter=self.adapter,
                live_preflight=verify_again)
            result = await runner.apply(
                order_id=order_id, x_plan_id=plan.id, organization_id=settings.moysklad_organization_id,
                x_store_id=plan.warehouse_id, items=hatuna_items,
                picker_confirmed=True)
            await self.confirm_x_receipt(order_id, plan.id, result["receipt_id"],
                                         simulated=result["simulated"])
        warehouse = WarehouseDocumentService(adapter=self.adapter, sessions=self.sessions)
        for plan in plans:
            # A successful shipment is terminal; a previously completed
            # reserve is read from the durable ledger.
            async with self.sessions() as session:
                refreshed = await session.get(Shipment, plan.id)
                current_order = await session.get(Order, order_id)
                reserve_key = plan_key(plan, "reserve", fake)
                operation = await session.get(ExternalOperation, reserve_key)
            if refreshed.status == "exported":
                continue
            if operation is None or operation.status != "succeeded":
                await warehouse.act(order_id, actor, chat, current_order.revision, "reserve", plan.id)
            async with self.sessions() as session:
                current_order = await session.get(Order, order_id)
            await warehouse.act(order_id, actor, chat, current_order.revision, "ship", plan.id)
        async with self.sessions() as session:
            current_order = await session.get(Order, order_id)
        from app.services.order_operations import OrderAction, OrderOperations
        await OrderOperations().act(order_id, actor,
            OrderAction(action="shipped", expected_revision=current_order.revision,
                        idempotency_key=f"hatuna:order:{order_id}:complete"))
        return {"order_id":order_id, "shipped":True, "warehouses":len(plans),
                "hatuna_receipts":len(x_plans), "simulated":fake}
