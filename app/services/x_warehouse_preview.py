"""Read-only preflight for X receipts. Site titles are search hints, never document IDs."""
from uuid import UUID

from sqlalchemy import select

from app.config.settings import settings
from app.database.session import async_session
from app.models.order import Order
from app.models.supply import Supplier, XSettlement
from app.services.order_desk import authorized_manager, require_owner, DeskError
from app.models.order_desk import OrderDesk
from app.services.order_source_service import source_plans
from app.services.product_service import ProductService
from app.services.telegram_display import telegram_rubles
from app.services.manager_workspace import site_titles_for


def real_moysklad_id(value):
    """Reject synthetic local references; MoySklad IDs are UUIDs."""
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)).casefold() == value.casefold()
    except (ValueError, AttributeError):
        return False


async def x_receipt_preflight(order_id, allocation_id, actor, chat_id):
    """Check exact Tilda->catalog binding and receipt prerequisites without writes."""
    async with async_session() as session, session.begin():
        await authorized_manager(session, actor, chat_id)
        desk = await session.scalar(select(OrderDesk).where(OrderDesk.order_id == order_id))
        if not desk:
            raise DeskError("Для оприходования нужна подтверждённая заявка сайта.")
        require_owner(desk, actor)
        from sqlalchemy.orm import selectinload
        order = await session.scalar(select(Order).where(Order.id == order_id)
                                     .options(selectinload(Order.items)))
        if not order:
            raise DeskError("Заказ не найден.")
        plans = await source_plans(session, order_id)
        pair = next(((p, a) for p in plans for a in p.allocations
                     if a.id == allocation_id and a.source_type == "partner_x"
                     and a.supply_status != "cancelled"), None)
        if not pair:
            raise DeskError("Действующий источник «Склад Х» не найден.")
        plan, allocation = pair
        item = next((i for i in order.items if i.id == allocation.order_item_id), None)
        if not item:
            raise DeskError("Позиция заказа отсутствует.")
        partner = await session.get(Supplier, allocation.supplier_id) if allocation.supplier_id else None
        settlement = await session.scalar(select(XSettlement).where(
            XSettlement.allocation_id == allocation.id))
        state = dict(order_revision=order.revision, plan_id=plan.id, quantity=allocation.qty,
                     warehouse_id=plan.warehouse_id, product_id=item.product_id,
                     product_name=item.name, sale_price_minor=item.price,
                     receipt_reference=allocation.receipt_reference,
                     physically_received=allocation.supply_status in {"received", "confirmed"},
                     already_posted=allocation.supply_status == "confirmed",
                     cost_minor=settlement.base_cost_minor if settlement else None,
                     source_name=partner.name if partner else "Склад Х")
        blockers = []
        if order.status == "rejected" or order.fulfillment_status != "new" or order.needs_review:
            blockers.append("Заказ закрыт, уже в сборке либо требует проверки.")
        if "test oms" in (order.customer_name or "").casefold() or "test oms" in (order.comment or "").casefold():
            blockers.append("Это тестовый заказ — реальные складские документы создавать нельзя.")
        if not state["physically_received"]:
            blockers.append("Сначала подтвердите фактическое получение товара со склада Х.")
        if state["already_posted"]:
            blockers.append("Поступление уже оформлено, повтор запрещён.")
        if not settlement:
            blockers.append("Закупочная себестоимость Х ещё не согласована.")
        if not partner or not real_moysklad_id(partner.external_reference):
            blockers.append("Нужно связать единственного партнёра Х с реальным контрагентом МойСклад (UUID).")
        if not real_moysklad_id(settings.moysklad_organization_id):
            blockers.append("Не настроена организация МойСклад.")
        if settings.warehouse_ids and plan.warehouse_id not in settings.warehouse_ids:
            blockers.append("Склад Х отсутствует в разрешённых складах.")
        try:
            catalog = {p["id"]: p for p in await ProductService().get_catalog_async(include_supply=False)}
            product = catalog.get(item.product_id)
            if not product or product.get("archived"):
                blockers.append("Привязанная позиция отсутствует в активном каталоге МойСклад.")
            elif plan.warehouse_id not in {s["id"] for s in product.get("stocks", [])}:
                blockers.append("Товар не сопоставлен с целевым складом МойСклад.")
            else:
                state["product_name"] = product.get("name") or state["product_name"]
        except Exception:
            blockers.append("Не удалось получить актуальный каталог МойСклад. Повторите проверку позже.")
        state["customer_counterparty_ready"] = real_moysklad_id(order.counterparty_id)
    original_names = await site_titles_for(order)
    site_name = original_names.get(item.id) or item.name
    lines = [
        f"Склад Х · подготовка оприходования · заказ №{order_id}",
        f"Сайт Tilda: {site_name[:180]}",
        f"Товар МойСклад: {state['product_name'][:180]}",
        f"Идентификатор товара: {state['product_id']}",
        f"Количество: {state['quantity']} шт.",
        f"Цена продажи: {telegram_rubles(state['sale_price_minor'])} за шт.",
        ("Себестоимость X: " + telegram_rubles(state["cost_minor"])
         if state["cost_minor"] is not None else "Себестоимость X: не согласована"),
        f"Физическое получение: {'подтверждено' if state['physically_received'] else 'не подтверждено'}",
        f"Документ МойСклад: {'уже создан' if state['already_posted'] else 'не создан'}",
        "После прихода: резерв → сборка → отгрузка.",
        ("Контрагент покупателя для будущего резерва: настроен"
         if state["customer_counterparty_ready"] else
         "Контрагент покупателя для будущего резерва: требуется привязка."),
    ]
    if blockers:
        lines.append("Что мешает реальному оприходованию:")
        lines.extend("• " + reason for reason in blockers)
    else:
        lines.append("Готово к оформлению реального документа поступления МойСклад.")
    return {"text": "\n".join(lines), "ready": not blockers, **state}
