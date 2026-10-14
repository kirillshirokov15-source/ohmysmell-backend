"""Manager-facing Hatuna accounting, sourced solely from committed CRM and ledgers."""
import os
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database.session import async_session
from app.models.order import Order
from app.models.order_desk import OrderDesk
from app.models.fulfillment import Shipment, WarehouseAllocation, ExternalOperation
from app.services.order_desk import authorized_manager, require_owner, DeskError
from app.services.telegram_display import telegram_rubles
from app.services.hatuna_accounting import hatuna_operation_keys, provisional_cost, HatunaPreflightError
from app.services.warehouse_documents import plan_key


async def order_hatuna_report(order_id, actor, chat_id, *, sessions=None):
    factory = sessions or async_session
    async with factory() as session:
        await authorized_manager(session, actor, chat_id)
        order = await session.scalar(select(Order).where(Order.id == order_id)
                .options(selectinload(Order.items)))
        desk = await session.scalar(select(OrderDesk).where(OrderDesk.order_id == order_id))
        if not order or not desk or not order.manual_fulfillment:
            raise DeskError("Нужен заказ Tilda с назначенным менеджером")
        require_owner(desk, actor)
        plans = list((await session.scalars(select(Shipment)
            .where(Shipment.order_id == order_id)
            .options(selectinload(Shipment.allocations))
            .order_by(Shipment.id))).all())
        items = {i.id: i for i in order.items}
        lines = [f"Учёт заказа №{order_id}",
                 f"Продажа Tilda: {telegram_rubles(order.total)}",
                 f"Статус: {order.fulfillment_status}"]
        for plan in plans:
            x = [a for a in plan.allocations
                 if a.source_type == "partner_x" and a.supply_status != "cancelled"]
            own = [a for a in plan.allocations
                   if a.source_type == "own" and a.supply_status != "cancelled"]
            kind = "Хатуна / склад Х" if x else "Основной склад" if own else "Другой"
            lines.append(f"\nПлан #{plan.id}: {kind} · {plan.status}")
            for alloc in x + own:
                item = items.get(alloc.order_item_id)
                if not item:
                    continue
                lines.append(f"• {item.name[:70]} · {alloc.qty} шт. "
                             f"· продажа {telegram_rubles(item.price)} / шт.")
                if alloc in x:
                    try:
                        cost = telegram_rubles(provisional_cost(item.price))
                    except HatunaPreflightError:
                        cost = "требует проверки"
                    lines.append(f"  Закупка Хатуна: {cost} / шт. (предварительно)")
            if x:
                keys = hatuna_operation_keys(order_id, plan.id)
                document_keys = [
                    ("Заказ поставщику", keys["purchase"]),
                    ("Приёмка на Х", keys["receipt"]),
                ]
            else:
                document_keys = []
            document_keys += [
                ("Резерв", plan_key(plan, "reserve", False)),
                ("Отгрузка", plan_key(plan, "ship", False)),
            ]
            for title, key in document_keys:
                op = await session.get(ExternalOperation, key)
                if not op:
                    op = await session.get(ExternalOperation, "simulation:" + key)
                status = ("не создан" if not op else
                          "подтверждён" if op.status == "succeeded" else
                          "ТРЕБУЕТ СВЕРКИ" if op.status == "uncertain" else
                          "выполняется")
                reference = (str(op.result.get("id"))[:50]
                             if op and isinstance(op.result, dict) and op.result.get("id") else "")
                lines.append(f"  {title}: {status}" + (f" · {reference}" if reference else ""))
        lines.append("\nСебестоимость Хатуны расчётная; сверить фактическую стоимость в конце месяца.")
        return "\n".join(lines)[:3900]
