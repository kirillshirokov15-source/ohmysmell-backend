"""Per-order X warehouse: one /enter, then one direct /demand in MoySklad.

No vendor, buying order, partner margin, synthetic counterparty, or guessed
product is created. Never rerun an uncertain provider call automatically.
"""
import logging
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config.settings import settings
from app.database.session import async_session
from app.integrations.write_guard import require_external_writes
from app.models.order import Order
from app.services.order_desk import DeskError
from app.services.order_source_service import OrderSourceService
from app.services.warehouse_documents import WarehouseDocumentService
from app.services.x_warehouse_preview import real_moysklad_id

logger = logging.getLogger(__name__)


def x_auto_blocked_reason(order, *, live=True):
    """Fail closed before any *new* accounting/stock write."""
    if not settings.x_auto_fulfill_enabled:
        return "Автоматическое оприходование и отгрузка Х пока не включены."
    if live:
        if settings.environment != "production" or not settings.external_writes_enabled:
            return "Реальные записи Х разрешены только в подготовленном production."
        if order.id in settings.x_auto_excluded_order_ids:
            return "Это тестовый заказ; автоматические документы МойСклад по нему запрещены."
        if settings.x_enter_unit_cost_minor is None:
            return "Не согласована учётная оценка оприходования Х (не цена продажи Tilda)."
        buyer = order.counterparty_id or settings.x_default_buyer_id
        if not real_moysklad_id(buyer):
            return "Не указан реальный UUID покупателя МойСклад для автоматической отгрузки."
    if order.status == "rejected" or order.fulfillment_status != "new" or order.needs_review:
        return "Заказ недоступен для автоматического исполнения."
    return None


class XWarehouseAutoService:
    def __init__(self, documents=None):
        self.documents = documents or WarehouseDocumentService()

    async def run(self, order_id, actor, chat_id, *, simulation=False):
        """Runs a confirmed X allocation to completion or returns blocked.

        Existing success is reused by the operations ledger. An unknown result
        raises and must be reconciled manually; no automatic retry takes place.
        """
        from app.integrations.moysklad.documents import FakeDocumentAdapter
        if simulation and type(self.documents.adapter) is not FakeDocumentAdapter:
            raise DeskError("Simulation needs explicit FakeDocumentAdapter.")
        if not simulation:
            require_external_writes()
        view = await OrderSourceService().read(order_id, actor, chat_id)
        async with async_session() as session:
            order = await session.scalar(select(Order).where(Order.id == order_id)
                                         .options(selectinload(Order.items)))
        if order is None:
            raise DeskError("Заказ не найден.")
        reason = x_auto_blocked_reason(order, live=not simulation)
        if reason:
            return dict(status="blocked", order_id=order_id, reason=reason)
        x_allocations = [a for a in view["allocations"]
                         if a["source_type"] == "partner_x" and a["status"] != "cancelled"]
        if not x_allocations:
            return dict(status="blocked", order_id=order_id,
                        reason="Для заказа не назначен склад Х.")
        other_in_x = {}
        for row in view["allocations"]:
            if row["status"] != "cancelled":
                other_in_x.setdefault(row["warehouse_id"], set()).add(row["source_type"])
        if any(other_in_x.get(row["warehouse_id"]) != {"partner_x"} for row in x_allocations):
            return dict(status="blocked", order_id=order_id,
                        reason="Складской план Х содержит другое назначение; нужна ручная проверка.")
        # Every individual X allocation receives a separate idempotent /enter.
        receipts = []
        for a in x_allocations:
            if a["status"] == "confirmed" and a["receipt_reference"] and not a["receipt_simulated"]:
                receipts.append(a["receipt_reference"])
                continue
            if a["status"] not in {"awaiting_receipt", "received"}:
                return dict(status="blocked", order_id=order_id,
                            reason="Оприходование Х находится в неопределённом статусе.")
            posted = await self.documents.act(order_id, actor, chat_id, view["revision"],
                                              "receipt", a["id"])
            receipts.append(posted["id"])
            view = await OrderSourceService().read(order_id, actor, chat_id)
        plan_ids = {a["plan_id"] for a in x_allocations}
        shipments = []
        for plan_id in sorted(plan_ids):
            view = await OrderSourceService().read(order_id, actor, chat_id)
            active = [a for a in view["allocations"] if a["plan_id"] == plan_id
                      and a["status"] != "cancelled"]
            if all(a["plan_status"] == "exported" for a in active):
                shipments.append(dict(plan_id=plan_id, status="already_exported"))
                continue
            if not all(a["status"] == "confirmed" and a["receipt_reference"] for a in active):
                return dict(status="partially_complete", receipts=receipts,
                            reason="Невозможно отгрузить: не все X поступления подтверждены.")
            shipped = await self.documents.act(order_id, actor, chat_id, view["revision"],
                                               "ship_x", plan_id)
            shipments.append(dict(plan_id=plan_id, document_id=shipped["id"]))
        logger.info("x_auto_documents_finished order_id=%s receipts=%s shipments=%s",
                    order_id, len(receipts), len(shipments))
        return dict(status="completed", order_id=order_id, receipts=receipts, shipments=shipments)
