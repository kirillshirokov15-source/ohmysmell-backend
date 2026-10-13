"""Own physical warehouse X: durable enter -> direct demand, no suppliers.

The customer sale price is immutable in OrderItem and is used ONLY for the
customer-facing demand. The MoySklad enter intentionally omits a purchase
price; if this MoySklad account rejects or values such a document
unexpectedly, the workflow fails CLOSED and never creates a demand.

External writes require the standard global guard and an independent,
production-only X_AUTO_FULFILLMENT_ENABLED flag. This module has no
automatic side effects on import, and does not process test orders.
"""
import asyncio
import logging
import os
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config.settings import settings
from app.database.session import async_session
from app.integrations.moysklad.documents import MoySkladDocumentAdapter
from app.integrations.moysklad.payloads import stock_enter, direct_demand
from app.integrations.write_guard import require_external_writes
from app.models.fulfillment import ExternalOperation, Shipment
from app.models.order import Order
from app.services.order_desk import DeskError
from app.services.external_operation_service import ExternalOperationConflict, ExternalOperationService
from app.services.order_source_service import source_plans

logger = logging.getLogger(__name__)


def uuid_id(value):
    try:
        return str(UUID(value)).lower() == value.lower()
    except (ValueError, TypeError, AttributeError):
        return False


def x_autoflow_enabled():
    return (settings.environment == "production"
        and settings.external_writes_enabled
        and os.getenv("X_AUTO_FULFILLMENT_ENABLED", "false").lower() == "true")


def x_enter_key(allocation_id):
    return f"xown:allocation:{allocation_id}:enter"


def x_demand_key(plan_id):
    return f"xown:shipment:{plan_id}:demand"


def test_order(order):
    text = " ".join((str(order.customer_name or ""),
                     str(order.comment or ""),
                     str(order.request_key or ""))).casefold()
    return any(token in text for token in ("test oms", "synthetic", "тестовый заказ"))


async def snapshot(order_id, *, require_buyer=True):
    """Read-only immutable preflight snapshot; NO provider writes."""
    from app.services.product_service import ProductService
    from app.models.order import OrderItem
    async with async_session() as session:
        order = await session.scalar(select(Order).where(Order.id == order_id)
            .options(selectinload(Order.items)))
        if not order or not order.manual_fulfillment or order.source != "website":
            raise DeskError("Автооперации Х разрешены только по подтверждённым заказам Tilda.")
        if order.status == "rejected" or order.needs_review or test_order(order):
            raise DeskError("Отменённый, тестовый или непроверенный заказ нельзя отправлять в МойСклад.")
        if order.fulfillment_status not in {"new", "shipped"}:
            raise DeskError("Автоотгрузка Х недоступна после ручного начала сборки.")
        if not uuid_id(settings.moysklad_organization_id):
            raise DeskError("Организация МойСклад не настроена.")
        if require_buyer and not uuid_id(order.counterparty_id):
            raise DeskError("Для автоматической отгрузки нужен контрагент покупателя МойСклад (UUID).")
        plans = await source_plans(session, order_id)
        rows = []
        by_id = {i.id: i for i in order.items}
        for plan in plans:
            portions = [a for a in plan.allocations if a.source_type == "partner_x"
                and a.managed_source and a.supply_status != "cancelled"]
            if not portions:
                continue
            if plan.status not in {"planned", "exported"}:
                raise DeskError("План склада Х недоступен.")
            if settings.warehouse_ids and plan.warehouse_id not in settings.warehouse_ids:
                raise DeskError("Склад Х отсутствует среди разрешённых складов.")
            parts = []
            for a in portions:
                item = by_id.get(a.order_item_id)
                if not item or not uuid_id(item.product_id):
                    raise DeskError("Товар с сайта не сопоставлен с конкретным UUID МойСклад.")
                if a.qty <= 0:
                    raise DeskError("Количество должно быть положительным.")
                parts.append(dict(allocation_id=a.id, item_id=item.id,
                     product_id=item.product_id, qty=a.qty,
                     retail_price_minor=item.price, receipt_id=a.receipt_reference,
                     supply_status=a.supply_status))
            rows.append(dict(plan_id=plan.id, store_id=plan.warehouse_id,
                status=plan.status, exported_id=plan.external_id, parts=parts))
        if not rows:
            return dict(order_id=order.id, plan_rows=[], order_revision=order.revision)
        captured = dict(order_id=order.id, organization_id=settings.moysklad_organization_id,
            counterparty_id=order.counterparty_id, order_revision=order.revision,
            plan_rows=rows)
    # Live catalog check is outside DB session and before any external document.
    catalog = {p["id"]: p for p in await ProductService().get_catalog_async(include_supply=False)}
    for plan in captured["plan_rows"]:
        for row in plan["parts"]:
            product = catalog.get(row["product_id"])
            if not product or product.get("archived"):
                raise DeskError("Сопоставленный товар отсутствует в актуальном каталоге МойСклад.")
            stores = {s["id"]: s for s in product.get("stocks", [])}
            store = stores.get(plan["store_id"])
            if not store or (store.get("name") or "").replace(" ", "").casefold() not in {"складх", "складx"}:
                raise DeskError("Источник Х не соответствует складу «Склад Х» в МойСклад.")
    return captured


async def _record_enter(order_id, allocation_id, document_id):
    """Reconcile a successful provider operation with CRM after a crash."""
    from app.models.fulfillment import WarehouseAllocation
    async with async_session() as session, session.begin():
        order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
        allocation = await session.get(WarehouseAllocation, allocation_id, with_for_update=True)
        if not order or not allocation or allocation.source_type != "partner_x":
            raise DeskError("Назначение склада Х было изменено; требуется ручная сверка.")
        if allocation.receipt_reference and allocation.receipt_reference != document_id:
            raise DeskError("Оприходование Х уже связано с другим документом.")
        if allocation.supply_status == "cancelled":
            raise DeskError("Назначение Х отменено после внешней операции; необходима сверка.")
        if allocation.receipt_reference != document_id or allocation.supply_status != "confirmed":
            allocation.receipt_reference = document_id
            allocation.receipt_simulated = False
            allocation.supply_status = "confirmed"
            order.revision += 1


async def _record_demand(order_id, plan_id, document_id):
    async with async_session() as session, session.begin():
        order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
        plan = await session.get(Shipment, plan_id, with_for_update=True)
        if not order or not plan or plan.order_id != order_id:
            raise DeskError("Складской план исчез после внешней операции.")
        if plan.external_id and plan.external_id != document_id:
            raise DeskError("Отгрузка этого плана уже связана с другим документом.")
        if plan.external_id != document_id or plan.status != "exported":
            plan.external_id = document_id
            plan.status = "exported"
            order.revision += 1
        # Mixed warehouse orders are not globally completed until every plan
        # is exported. Payment is untouched.
        plans = (await session.scalars(select(Shipment).where(
            Shipment.order_id == order_id, Shipment.status != "cancelled"))).all()
        if plans and all(p.status == "exported" for p in plans):
            order.fulfillment_status = "shipped"


class XOwnWarehouseAutomation:
    def __init__(self, *, adapter=None, operations=None):
        self.adapter = adapter or MoySkladDocumentAdapter()
        self.operations = operations or ExternalOperationService()

    async def process_order(self, order_id, *, dry_run=True):
        """Idempotent sequence; no demand is sent until every enter is verified.

        dry_run always returns a plan. To actually mutate MoySklad requires a
        double guard: environment + external_writes + separate explicit flag.
        """
        data = await snapshot(order_id, require_buyer=not dry_run)
        if dry_run:
            return dict(mode="preview", **data)
        if not x_autoflow_enabled():
            raise DeskError("Автооприходование Х отключено отдельным production-переключателем.")
        require_external_writes()
        if not data["plan_rows"]:
            return dict(mode="no_x", order_id=order_id)
        result_rows = []
        for plan in data["plan_rows"]:
            if plan["status"] == "exported":
                result_rows.append(dict(plan_id=plan["plan_id"], status="already_shipped"))
                continue
            for row in plan["parts"]:
                key = x_enter_key(row["allocation_id"])
                intent = {"entity": "enter",
                    "document": stock_enter(data["organization_id"], plan["store_id"],
                        [{"id": row["product_id"], "qty": row["qty"]}], key)}
                }
                result = await self.operations.execute(key, intent, self.adapter.create)
                await _record_enter(order_id, row["allocation_id"], result["id"])
            # Read the current allocation snapshot to verify that all enter
            # documents succeeded before emitting an outgoing shipment.
            for row in plan["parts"]:
                key = x_enter_key(row["allocation_id"])
                record = await self.operations.store.get(key)
                if not record or record["status"] != "succeeded" or not record["result"]:
                    raise DeskError("Оприходование Х не подтверждено. Отгрузка запрещена.")
            key = x_demand_key(plan["plan_id"])
            lines = [{"id": row["product_id"], "qty": row["qty"],
                      "price": row["retail_price_minor"]} for row in plan["parts"]]
            intent = {"entity": "demand",
                "document": direct_demand(data["organization_id"],
                    data["counterparty_id"], plan["store_id"], lines, key)}
            result = await self.operations.execute(key, intent, self.adapter.create)
            await _record_demand(order_id, plan["plan_id"], result["id"])
            result_rows.append(dict(plan_id=plan["plan_id"], status="shipped",
                                    enter_count=len(plan["parts"]), demand_id=result["id"]))
        return dict(mode="live", order_id=order_id, results=result_rows)


async def x_auto_loop():
    """Manager-worker polling; disabled unless independently activated."""
    from app.models.fulfillment import WarehouseAllocation
    from app.database.session import async_session
    while True:
        try:
            if x_autoflow_enabled():
                async with async_session() as session:
                    order_ids = (await session.scalars(
                        select(Shipment.order_id).join(WarehouseAllocation).join(Order)
                        .where(WarehouseAllocation.source_type == "partner_x",
                               WarehouseAllocation.managed_source.is_(True),
                               WarehouseAllocation.supply_status != "cancelled",
                               Shipment.status == "planned", Order.status != "rejected",
                               Order.needs_review.is_(False),
                               Order.manual_fulfillment.is_(True))
                        .distinct().limit(20))).all()
                for order_id in order_ids:
                    try:
                        await XOwnWarehouseAutomation().process_order(order_id, dry_run=False)
                    except (DeskError, ExternalOperationConflict) as error:
                        logger.warning("x_auto_blocked order_id=%s reason=%s",
                                       order_id, type(error).__name__)
                    except Exception:
                        logger.exception("x_auto_uncertain order_id=%s", order_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("x_auto_loop_error")
        await asyncio.sleep(30)
