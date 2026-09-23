"""Transactional local procurement; no external write clients are imported."""
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol
from sqlalchemy import select
from app.database.session import async_session
from app.models.order import Order, OrderItem
from app.models.manager import Manager
from app.models.supply import Supplier, ProductSupply, SupplierOffer, OrderItemSupply, XSettlement, ProcurementRequest, SupplyEvent
from app.services.fx import CbrFxProvider, FxQuote, convert_minor, rate_value
from app.logging_utils import log_event

LABELS = {"own": "Наш склад", "partner_x": "X-склад", "external": "Внешние поставщики"}
TRANSITIONS = {"needed": {"requested", "cancelled", "unavailable"},
    "requested": {"confirmed", "cancelled", "unavailable"},
    "confirmed": {"received", "cancelled", "unavailable"}, "received": set(), "cancelled": set(), "unavailable": set()}


class SupplyError(ValueError):
    pass


class SupplierCommunication(Protocol):
    """Future adapter; marking 'requested' currently records the manager's manual action."""
    async def send_request(self, procurement_id: int, idempotency_key: str) -> str: ...


def x_calculation(base_cost_minor, sale_price_minor):
    for value in (base_cost_minor, sale_price_minor):
        if type(value) is not int or not 0 <= value <= 9_223_372_036_854_775_807:
            raise SupplyError("Стоимость должна быть целым неотрицательным числом")
    margin = sale_price_minor - base_cost_minor
    return dict(base_cost_minor=base_cost_minor, sale_price_minor=sale_price_minor,
        total_margin_minor=margin, partner_margin_minor=margin // 2 if margin >= 0 else None,
        our_margin_minor=margin - margin // 2 if margin >= 0 else None,
        partner_due_minor=base_cost_minor + margin // 2 if margin >= 0 else None,
        status="calculated" if margin >= 0 else "requires_financial_review")


async def capture_order_supply(session, order):
    """Called inside BOTH order creation transactions, after items have IDs."""
    configs = {s.product_id: s for s in (await session.scalars(select(ProductSupply)
        .where(ProductSupply.product_id.in_([i.product_id for i in order.items])))).all()}
    for item in order.items:
        if await session.get(OrderItemSupply, item.id):
            continue
        config = configs.get(item.product_id)
        source = config.source_type if config else "own"
        snapshot = OrderItemSupply(order_item_id=item.id, order_id=order.id, source_type=source)
        session.add(snapshot)
        if source == "external":
            session.add(ProcurementRequest(order_id=order.id, order_item_id=item.id))
        elif source == "partner_x":
            supplier = await session.get(Supplier, config.supplier_id)
            if not supplier or supplier.supplier_type != "partner_x" or supplier.status != "active":
                raise SupplyError("Партнёр X не настроен")
            values = x_calculation(config.base_cost_minor * item.qty, item.item_total)
            snapshot.supplier_id, snapshot.supplier_name = supplier.id, supplier.name
            snapshot.cost_snapshot = {"original_purchase_price_minor": config.base_cost_minor,
                "original_currency": "RUB", "quantity": item.qty, "fx_rate_to_rub": "1",
                "converted_purchase_cost_rub_minor": values["base_cost_minor"],
                "sale_price_minor": item.item_total, "margin_rub_minor": values["total_margin_minor"]}
            session.add(XSettlement(order_id=order.id, order_item_id=item.id, supplier_id=supplier.id, **values))
            if values["status"] == "requires_financial_review":
                order.needs_review = True


async def catalog_supply(catalog):
    """Only availability/source labels: never put supplier finance in the catalog."""
    async with async_session() as session:
        configs = {s.product_id: s.source_type for s in (await session.scalars(select(ProductSupply))).all()}
        offers = list((await session.scalars(select(SupplierOffer).join(Supplier)
            .where(SupplierOffer.active.is_(True), Supplier.status == "active"))).all())
    result = []
    for original in catalog:
        product = dict(original)
        source = configs.get(product["id"], "own")
        product["supply_source"] = source
        if source == "external":
            states = [o.availability for o in offers if o.product_id == product["id"]]
            product["supply_availability"] = "confirmed" if "confirmed" in states else "on_request" if "on_request" in states else "unavailable"
            product.update(stocks=[], total_stock=0, total_reserve=0, total_available=0)
        result.append(product)
    return result


class ProcurementService:
    def __init__(self, fx=None):
        self.fx = fx or CbrFxProvider()

    async def refresh_estimate(self, offer_id):
        async with async_session() as session:
            offer = await session.get(SupplierOffer, offer_id)
            if not offer:
                raise SupplyError("Предложение не найдено")
            currency = offer.currency_code
        quote = await asyncio.to_thread(self.fx.quote, currency)
        async with async_session() as session, session.begin():
            offer = await session.get(SupplierOffer, offer_id, with_for_update=True)
            if offer.currency_code != quote.currency:
                raise SupplyError("Предложение изменилось; повторите")
            offer.current_fx_rate_to_rub, offer.fx_source, offer.fx_rate_date = quote.rate, quote.source, quote.rate_date
            offer.estimated_purchase_cost_rub_minor = convert_minor(offer.purchase_price_minor, quote.rate)
            return offer

    async def act(self, procurement_id, telegram_id, action, expected_revision, *, offer_id=None, manual_rate=None):
        # All writers lock Order then Procurement, matching order lifecycle lock order.
        async with async_session() as session, session.begin():
            manager = await session.scalar(select(Manager).where(Manager.telegram_id == telegram_id, Manager.is_active.is_(True)))
            if not manager:
                raise SupplyError("Нет доступа менеджера")
            order_id = await session.scalar(select(ProcurementRequest.order_id).where(ProcurementRequest.id == procurement_id))
            if order_id is None:
                raise SupplyError("Закупка не найдена")
            order = await session.get(Order, order_id, with_for_update=True)
            request = await session.get(ProcurementRequest, procurement_id, with_for_update=True)
            if request.revision != expected_revision:
                # Replay only the exact recorded effect from this manager/version.
                events = list((await session.scalars(select(SupplyEvent).where(SupplyEvent.procurement_id == request.id,
                    SupplyEvent.manager_id == manager.id, SupplyEvent.action == action))).all())
                details = {"revision": expected_revision, "offer_id": offer_id, "manual_rate": str(manual_rate) if manual_rate is not None else None}
                if any(e.details == details for e in events):
                    return request
                raise SupplyError("Карточка устарела; обновите закупку")
            if order.fulfillment_status in {"cancelled", "shipped"}:
                raise SupplyError("Закупка закрытого заказа недоступна")
            item = await session.get(OrderItem, request.order_item_id)
            snapshot = await session.get(OrderItemSupply, item.id, with_for_update=True)
            now = datetime.now(timezone.utc)
            if action == "select":
                if request.status != "needed":
                    raise SupplyError("Поставщика можно выбрать до отправки запроса")
                offer = await session.get(SupplierOffer, offer_id)
                supplier = await session.get(Supplier, offer.supplier_id) if offer else None
                if not offer or offer.product_id != item.product_id or not offer.active or offer.availability == "unavailable" or not supplier or supplier.status != "active" or supplier.supplier_type != "external_wholesaler":
                    raise SupplyError("Предложение поставщика недоступно для этой позиции")
                if offer.availability_qty is not None and offer.availability_qty < item.qty:
                    raise SupplyError("Поставщик указал недостаточное количество")
                request.offer_id, request.supplier_id = offer.id, supplier.id
                request.manual_fx_rate = request.manual_fx_by_manager_id = request.manual_fx_at = None
            elif action == "fx":
                if request.status not in {"needed", "requested"} or not request.offer_id:
                    raise SupplyError("Курс фиксируется до подтверждения закупки, после выбора поставщика")
                offer = await session.get(SupplierOffer, request.offer_id)
                quote = FxQuote(offer.currency_code, rate_value(manual_rate), "manual", now.date())
                request.manual_fx_rate, request.manual_fx_by_manager_id, request.manual_fx_at = quote.rate, manager.id, now
            else:
                if action not in TRANSITIONS[request.status]:
                    raise SupplyError("Недопустимый переход закупки")
                if action in {"requested", "confirmed"} and not request.offer_id:
                    raise SupplyError("Сначала выберите поставщика")
                if action == "confirmed":
                    offer = await session.get(SupplierOffer, request.offer_id, with_for_update=True)
                    supplier = await session.get(Supplier, request.supplier_id)
                    if not offer.active or offer.product_id != item.product_id or offer.supplier_id != request.supplier_id or offer.availability == "unavailable" or supplier.status != "active" or (offer.availability_qty is not None and offer.availability_qty < item.qty):
                        raise SupplyError("Наличие поставщика требует проверки")
                    manual = request.manual_fx_rate is not None
                    rate = request.manual_fx_rate if manual else offer.current_fx_rate_to_rub
                    if offer.currency_code == "RUB":
                        rate = Decimal("1")
                    if rate is None:
                        raise SupplyError("Обновите курс ЦБ или укажите ручной курс")
                    fixed = convert_minor(offer.purchase_price_minor * item.qty, rate)
                    snapshot.supplier_id, snapshot.supplier_name = supplier.id, supplier.name
                    snapshot.cost_snapshot = {"original_purchase_price_minor": offer.purchase_price_minor,
                        "original_currency": offer.currency_code, "quantity": item.qty, "fx_rate_to_rub": str(rate),
                        "fx_source": "manual" if manual else ("identity" if offer.currency_code == "RUB" else offer.fx_source),
                        "fx_rate_date": str(request.manual_fx_at.date() if manual else offer.fx_rate_date or now.date()),
                        "fx_manager_id": request.manual_fx_by_manager_id if manual else None,
                        "fixed_at": now.isoformat(), "converted_purchase_cost_rub_minor": fixed,
                        "sale_price_minor": item.item_total, "margin_rub_minor": item.item_total - fixed,
                        "supplier_sku": offer.supplier_sku}
                    request.confirmed_at = now
                request.status = action
            session.add(SupplyEvent(procurement_id=request.id, manager_id=manager.id, action=action,
                details={"revision": expected_revision, "offer_id": offer_id, "manual_rate": str(manual_rate) if manual_rate is not None else None}))
            request.revision += 1
            await session.flush()
            log_event(logging.getLogger(__name__), "procurement_changed", order_id=order.id,
                manager_id=manager.id, action=action, result="success")
            return request


async def procurement_data(order_id=None):
    async with async_session() as session:
        query = select(ProcurementRequest)
        if order_id is not None:
            query = query.where(ProcurementRequest.order_id == order_id)
        else:
            query = query.where(ProcurementRequest.status.in_(["needed", "requested", "confirmed", "unavailable"]))
        return list((await session.scalars(query.order_by(ProcurementRequest.id.desc()).limit(50))).all())


async def ready_supply_quantities(session, order_id):
    rows = list((await session.scalars(select(OrderItemSupply).where(OrderItemSupply.order_id == order_id))).all())
    requests = {r.order_item_id: r.status for r in (await session.scalars(select(ProcurementRequest).where(ProcurementRequest.order_id == order_id))).all()}
    result = {}
    for row in rows:
        if row.source_type == "external":
            if requests.get(row.order_item_id) != "received":
                raise SupplyError("Сначала получите товары внешнего поставщика")
            item = await session.get(OrderItem, row.order_item_id)
            result[item.id] = item.qty
    return result
