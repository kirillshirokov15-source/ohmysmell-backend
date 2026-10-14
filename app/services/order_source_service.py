"""Explicit Tilda source portions on existing warehouse plans and Buying purchases.

No provider writes. X/external receiving and posting are labelled simulations;
production cannot use their receipts as shipment evidence.
"""
import hashlib
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.config.settings import settings
from app.database.session import async_session
from app.models.order import Order, OrderItem
from app.models.order_desk import OrderDesk
from app.models.fulfillment import Shipment, WarehouseAllocation
from app.models.operations import OrderEvent
from app.models.buying import BuyingSupplier
from app.models.supply import Supplier, SupplierOffer, XSettlement
from app.services.checkout_service import transaction_lock
from app.services.order_desk import authorized_manager, require_owner, DeskError
from app.services.product_service import ProductService
from app.services.stock_allocation import available_units
from app.services.supply_service import x_calculation
from app.services import buying


def simulation_allowed():
    return settings.environment != "production" and not settings.external_writes_enabled


def allocation_ready(allocation, *, manual=False):
    if not allocation.managed_source:
        return not manual
    if allocation.supply_status != "confirmed":
        return False
    if allocation.source_type == "own":
        return True
    if not allocation.receipt_reference:
        return False
    if allocation.receipt_simulated and not simulation_allowed():
        return False
    if allocation.source_type == "external":
        purchase = allocation.buying_purchase
        if not purchase or purchase.status != "received":
            return False
    return True


async def source_plans(session, order_id):
    return list((await session.scalars(select(Shipment).where(Shipment.order_id == order_id)
        .options(selectinload(Shipment.allocations).selectinload(WarehouseAllocation.buying_purchase))
        .order_by(Shipment.id))).all())


async def lock_source_stock(session, keys):
    for pid, warehouse in sorted(set(keys)):
        await transaction_lock(session, f"source-stock:{pid}:{warehouse}")


async def claimed_stock(session, product_id, warehouse_id, *, managed_only=False):
    query = (select(WarehouseAllocation)
        .join(Shipment).join(Order, Order.id == Shipment.order_id)
        .join(OrderItem, OrderItem.id == WarehouseAllocation.order_item_id)
        .where(OrderItem.product_id == product_id, Shipment.warehouse_id == warehouse_id,
            Shipment.status != "cancelled", Order.fulfillment_status != "cancelled", Order.status != "rejected",
            WarehouseAllocation.supply_status != "cancelled", WarehouseAllocation.source_type == "own"))
    if managed_only:
        query = query.where(WarehouseAllocation.managed_source.is_(True))
    allocations=list((await session.scalars(query)).all())
    # Provider stock reports already subtract real reservations and shipments.
    # Keep uncertain and simulated operations as local claims; never subtract twice.
    from app.models.fulfillment import ExternalOperation
    from app.services.warehouse_documents import plan_key
    plans={}
    claimed=0
    for allocation in allocations:
        identifier=allocation.shipment_id
        if identifier not in plans:
            plan=await session.scalar(select(Shipment).where(Shipment.id==identifier)
                .options(selectinload(Shipment.allocations)))
            reserved=await session.get(ExternalOperation,plan_key(plan,'reserve',False))
            released=await session.get(ExternalOperation,plan_key(plan,'release',False))
            plans[identifier]=bool(reserved and reserved.status=='succeeded' and not released)
        if not plans[identifier]:
            claimed+=allocation.qty
    return claimed


async def attach_x_cost_status(session, view):
    """Financial state is separate from retail sale and physical source."""
    ids = [a["id"] for a in view["allocations"]
           if a["source_type"] == "partner_x" and a["status"] != "cancelled"]
    if not ids:
        return view
    settlements = (await session.scalars(
        select(XSettlement).where(XSettlement.allocation_id.in_(ids)))).all()
    known = {row.allocation_id: row for row in settlements}
    for allocation in view["allocations"]:
        if allocation["id"] in ids:
            settlement = known.get(allocation["id"])
            allocation["x_cost_pending"] = settlement is None
            allocation["x_total_cost_minor"] = settlement.base_cost_minor if settlement else None
    return view


async def singleton_x_partner(session):
    """Only one operational X counterparty. Created locally on explicit assignment.

    This is a CRM supplier row, NOT a MoySklad counterparty, purchase, or receipt.
    Advisory locking prevents duplicate creations by simultaneous managers.
    """
    await transaction_lock(session, "oms:partner-x:singleton")
    active = list((await session.scalars(
        select(Supplier).where(Supplier.supplier_type == "partner_x",
                               Supplier.status == "active").order_by(Supplier.id))).all())
    preferred = [s for s in active
                 if s.external_reference == "oms:partner-x:singleton"
                 or s.name.strip().casefold() in {"склад х", "склад x"}]
    if len(active) == 1:
        return active[0]
    if len(preferred) == 1:
        return preferred[0]
    if len(active) > 1:
        raise DeskError(
            "Настроено несколько активных партнёров Х. "
            "Оставьте одного (Склад Х) в CRM, чтобы не создать неправильные взаиморасчёты.")
    partner = Supplier(supplier_type="partner_x", name="Склад Х",
                       status="active", external_reference="oms:partner-x:singleton")
    session.add(partner)
    await session.flush()
    return partner


class OrderSourceService:
    def __init__(self, products=None):
        self.products = products

    async def _order(self, session, order_id, actor, chat_id):
        manager = await authorized_manager(session, actor, chat_id)
        order = await session.scalar(select(Order).where(Order.id == order_id)
            .options(selectinload(Order.items)).with_for_update())
        desk = await session.scalar(select(OrderDesk).where(OrderDesk.order_id == order_id))
        if not order or not order.manual_fulfillment or not desk:
            raise DeskError("Нужен подтверждённый заказ Tilda.")
        require_owner(desk, actor)
        return order, manager

    async def read(self, order_id, actor, chat_id):
        async with async_session() as session, session.begin():
            order, _ = await self._order(session, order_id, actor, chat_id)
            plans = await source_plans(session, order_id)
            view = self.view(order, plans)
            return await attach_x_cost_status(session, view)

    @staticmethod
    def view(order, plans):
        allocations = []
        assigned, ready = defaultdict(int), defaultdict(int)
        for plan in plans:
            for a in plan.allocations:
                active = plan.status != "cancelled" and a.supply_status != "cancelled"
                confirmed = active and plan.status in {"planned", "exported"} and allocation_ready(a, manual=True)
                if active:
                    assigned[a.order_item_id] += a.qty
                if confirmed:
                    ready[a.order_item_id] += a.qty
                purchase = a.buying_purchase
                allocations.append(dict(id=a.id, plan_id=plan.id, item_id=a.order_item_id, qty=a.qty,
                    source_type=a.source_type, warehouse_id=plan.warehouse_id, plan_status=plan.status,
                    status=a.supply_status, supplier_id=a.supplier_id, ready=confirmed,
                    purchase_id=a.buying_purchase_id, purchase_status=purchase.status if purchase else None,
                    requires_purchase=a.source_type == "external" and (not purchase or purchase.status == "draft"),
                    receipt_simulated=a.receipt_simulated, receipt_reference=a.receipt_reference))
        return dict(order_id=order.id, revision=order.revision, allocations=allocations,
            items=[dict(id=i.id, name=i.name, qty=i.qty, sale_unit_minor=i.price,
                assigned=assigned[i.id], ready=ready[i.id]) for i in order.items])

    async def act(self, order_id, actor, chat_id, request):
        # Preserve historical request hashes for all pre-existing actions.
        # The new optional X cost field must not change legacy idempotency checks.
        excluded = {"idempotency_key"}
        if request.action != "set_x_cost":
            excluded.add("x_unit_cost_minor")
        digest = hashlib.sha256(request.model_dump_json(exclude=excluded).encode()).hexdigest()
        async with async_session() as session, session.begin():
            order, manager = await self._order(session, order_id, actor, chat_id)
            previous = await session.scalar(select(OrderEvent).where(OrderEvent.order_id == order_id,
                OrderEvent.idempotency_key == request.idempotency_key))
            if previous:
                if previous.request_hash != digest or previous.manager_id != manager.id:
                    raise DeskError("Ключ операции уже использован.")
                return previous.after
            if order.revision != request.expected_revision:
                raise DeskError("Карточка устарела; обновите заказ.")
            if order.status == "rejected" or order.fulfillment_status != "new" or order.needs_review:
                raise DeskError("Источники меняются до сборки, после завершения проверки заказа.")
            plans = await source_plans(session, order_id)
            before = self.view(order, plans)
            if request.action in {'assign','cancel','set_x_cost'}:
                from app.services.warehouse_documents import frozen_plan
                if any([await frozen_plan(session,p) for p in plans]):
                    raise DeskError('Сначала сверить и снять внешний резерв.')
            if request.action == "assign":
                await self._assign(session, order, plans, request.parts, actor)
            else:
                pair = next(((p, a) for p in plans for a in p.allocations if a.id == request.allocation_id), None)
                if not pair or not pair[1].managed_source:
                    raise DeskError("Распределение не найдено.")
                plan, allocation = pair
                if plan.status != "planned" or allocation.supply_status == "cancelled":
                    raise DeskError("Складской план или распределение недоступны.")
                if request.action == "set_x_cost":
                    if allocation.source_type != "partner_x" or allocation.supply_status not in {"awaiting_receipt", "received"}:
                        raise DeskError("Закупочная цена доступна для Х до оприходования, включая физически полученный товар.")
                    if allocation.receipt_reference:
                        raise DeskError("Товар уже оприходован; менять себестоимость нельзя.")
                    current = await session.scalar(select(XSettlement).where(XSettlement.allocation_id == allocation.id))
                    if current:
                        raise DeskError("Цена склада Х уже подтверждена; корректировка требует отдельной финансовой процедуры.")
                    item = next((i for i in order.items if i.id == allocation.order_item_id), None)
                    if not item:
                        raise DeskError("Позиция заказа не найдена.")
                    values = x_calculation(request.x_unit_cost_minor * allocation.qty, item.price * allocation.qty)
                    if values["status"] != "calculated":
                        raise DeskError("Себестоимость Х выше розничной цены Tilda. Нужна отдельная финансовая проверка.")
                    if not allocation.supplier_id:
                        raise DeskError("У распределения склада Х отсутствует локальный контрагент.")
                    session.add(XSettlement(order_id=order.id, order_item_id=item.id,
                        allocation_id=allocation.id, supplier_id=allocation.supplier_id, **values))
                elif request.action == "cancel":
                    allocation.supply_status = "cancelled"
                    await session.flush()
                    if allocation.buying_purchase_id:
                        await buying.cancel_unsubmitted_purchase(session, allocation.buying_purchase_id, f"telegram:{actor}")
                    if all(a.supply_status == "cancelled" for a in plan.allocations):
                        plan.status = "cancelled"
                else:
                    if not simulation_allowed() and not (request.action == 'receive' and
                            settings.environment == 'production' and settings.external_writes_enabled):
                        raise DeskError("Оприходование пока доступно только в симуляции без внешних записей.")
                    if allocation.source_type == "own":
                        raise DeskError("Собственный остаток уже проверен при назначении.")
                    expected = "awaiting_receipt" if request.action == "receive" else "received"
                    if allocation.supply_status != expected:
                        raise DeskError("Сначала выполните предыдущий этап получения.")
                    if allocation.source_type == "partner_x" and request.action == "post_receipt":
                        verified_cost = await session.scalar(select(XSettlement.id).where(
                            XSettlement.allocation_id == allocation.id))
                        if not verified_cost:
                            raise DeskError("Для оприходования склада Х сначала подтвердите реальную закупочную себестоимость.")
                    if allocation.source_type == "external":
                        # Same workspace lock as Buying receive/cancel. A draft or
                        # merely sent purchase is never proof of physical receipt.
                        await buying.lock(session)
                        if not allocation.buying_purchase:
                            raise DeskError("Связанная закупка Buying отсутствует.")
                        await session.refresh(allocation.buying_purchase)
                        if allocation.buying_purchase.status != "received":
                            raise DeskError("Сначала отметьте получение этой закупки в Buying.")
                    allocation.supply_status = "received" if request.action == "receive" else "confirmed"
                    allocation.receipt_simulated = simulation_allowed()
                    if request.action == "post_receipt":
                        allocation.receipt_reference = f"simulated-receipt:allocation:{allocation.id}"
            order.revision += 1
            manager_id = manager.id
            order.status_changed_at = datetime.now(timezone.utc)
            order.status_changed_by_manager_id = manager.id
            await session.flush()
            # Reload newly created purchase relationships without implicit async IO.
            session.expire_all()
            order = await session.scalar(select(Order).where(Order.id == order_id).options(selectinload(Order.items)))
            after = await attach_x_cost_status(session, self.view(order, await source_plans(session, order_id)))
            session.add(OrderEvent(order_id=order_id, manager_id=manager_id, action="source_" + request.action,
                idempotency_key=request.idempotency_key, request_hash=digest, before=before, after=after))
            return after

    async def _assign(self, session, order, plans, parts, actor):
        import os
        if (os.getenv("OMS_HATUNA_POST_PICK_ENABLED", "false").lower() == "true"
                and any(part.source_type == "external" for part in parts)):
            raise DeskError("Заказы Tilda обеспечиваются только основным складом и складом Х. Buying отдельно.")
        items = {i.id: i for i in order.items}
        assigned = defaultdict(int)
        for plan in plans:
            if plan.status != "cancelled":
                for a in plan.allocations:
                    if a.supply_status != "cancelled":
                        assigned[a.order_item_id] += a.qty
        for part in parts:
            if part.item_id not in items:
                raise DeskError("Позиция не принадлежит заказу.")
            assigned[part.item_id] += part.qty
            if assigned[part.item_id] > items[part.item_id].qty:
                raise DeskError("Распределённое количество превышает количество позиции.")
        # Serialize claims on physical stock across orders, in deterministic order.
        await lock_source_stock(session, [(items[p.item_id].product_id, p.warehouse_id) for p in parts if p.source_type == "own"])
        if any(p.source_type == "external" for p in parts):
            await buying.lock(session)
        products = self.products or ProductService()
        try:
            catalog = {p["id"]: p for p in await products.get_catalog_async(
                include_supply=False, fresh_stock=True)}
        except Exception as error:
            raise DeskError("Не удалось проверить актуальные остатки и склады.") from error
        by_warehouse = {p.warehouse_id: p for p in plans}
        for part in parts:
            item = items[part.item_id]
            product = catalog.get(item.product_id)
            stores = {s["id"]: s for s in product.get("stocks", [])} if product else {}
            if part.warehouse_id not in stores or (settings.warehouse_ids and part.warehouse_id not in settings.warehouse_ids):
                raise DeskError("Товар или склад отсутствует в актуальном каталоге.")
            if part.source_type == "own":
                stock = stores[part.warehouse_id]
                claimed = await claimed_stock(session, item.product_id, part.warehouse_id)
                if part.qty > available_units(stock.get("stock"), stock.get("reserve")) - claimed:
                    raise DeskError("Недостаточно свободного остатка с учётом локальных распределений.")
            plan = by_warehouse.get(part.warehouse_id)
            if plan and plan.status not in {"planned", "cancelled"}:
                raise DeskError("План уже передан на внешнее исполнение.")
            if plan is None:
                plan = Shipment(order_id=order.id, warehouse_id=part.warehouse_id, allocations=[])
                session.add(plan)
                by_warehouse[part.warehouse_id] = plan
            elif plan.status == "cancelled":
                # Never reactivate quantities from a cancelled historical plan.
                for old in plan.allocations:
                    old.supply_status = "cancelled"
                plan.status = "planned"
            allocation = WarehouseAllocation(order_item_id=item.id, qty=part.qty, managed_source=True,
                source_type=part.source_type, supply_status="confirmed" if part.source_type == "own" else "awaiting_receipt")
            plan.allocations.append(allocation)
            await session.flush()
            if part.source_type == "partner_x":
                # Backward-compatible database value: X means our own physical
                # warehouse, never an external counterparty or purchase.
                # Legacy records remain readable but new X allocations do not
                # create supplier rows or XSettlements.
                if part.supplier_id is not None or part.base_cost_minor is not None:
                    raise DeskError("Склад Х — собственный склад. Поставщик и закупочная цена не нужны.")
                allocation.supplier_id = None
            elif part.source_type == "external":
                offer = await session.get(SupplierOffer, part.offer_id)
                supplier = await session.get(Supplier, offer.supplier_id) if offer else None
                if (not offer or offer.product_id != item.product_id or not offer.active or offer.availability == "unavailable"
                        or not supplier or supplier.status != "active" or supplier.supplier_type != "external_wholesaler"
                        or not supplier.email or (offer.availability_qty is not None and offer.availability_qty < part.qty)):
                    raise DeskError("Предложение поставщика недоступно для выбранного количества.")
                workspace_supplier = await session.get(BuyingSupplier, supplier.id)
                if not workspace_supplier or workspace_supplier.currency != offer.currency_code:
                    raise DeskError("Настройте поставщика и валюту предложения в Buying.")
                entry = dict(offer_id=offer.id, product_id=item.product_id, name=item.name, quantity=part.qty,
                    unit_price_minor=offer.purchase_price_minor, approximate_rub_minor=None,
                    fx_rate_to_rub=None, fx_source=None, fx_rate_date=None)
                snapshot = dict(supplier_id=supplier.id, supplier_name=supplier.name, recipient=supplier.email,
                    items=[entry], currency=offer.currency_code, total_minor=offer.purchase_price_minor * part.qty,
                    approximate_rub_minor=None, approximate=True, email_body=f"{item.name} – {part.qty} шт.")
                if part.purchase_id:
                    purchase = await buying.purchase_get(session, part.purchase_id)
                    from app.models.fulfillment import ExternalOperation
                    standalone_key=f'buying:purchase:{purchase.id}:receipt'
                    if (purchase.external_ids.get('warehouse_receipt') or
                        await session.get(ExternalOperation,standalone_key) or
                        await session.get(ExternalOperation,'simulation:'+standalone_key)):
                        raise DeskError('Закупка уже оприходована или сверяется: используйте собственный склад после проверки остатка.')
                    if purchase.supplier_id != supplier.id or purchase.status not in {'draft','sent','confirmed','received'}:
                        raise DeskError('Закупка недоступна для распределения.')
                    purchased = sum(row['quantity'] for row in purchase.snapshot['items']
                                    if row['product_id'] == item.product_id and row['offer_id'] == offer.id)
                    # Include completed shipments: delivered goods cannot supply a second order.
                    claimed = await session.scalar(select(func.coalesce(func.sum(WarehouseAllocation.qty),0))
                        .join(OrderItem,OrderItem.id==WarehouseAllocation.order_item_id).join(Shipment)
                        .where(WarehouseAllocation.buying_purchase_id==purchase.id,
                               OrderItem.product_id==item.product_id,
                               WarehouseAllocation.supply_status!='cancelled',Shipment.status!='cancelled'))
                    if part.qty > purchased - claimed:
                        raise DeskError('Количество закупки уже распределено или товар не совпадает.')
                else:
                    purchase = await buying.create_purchase(session, snapshot, f"telegram:{actor}")
                allocation.buying_purchase_id, allocation.supplier_id = purchase.id, supplier.id
            await session.flush()
