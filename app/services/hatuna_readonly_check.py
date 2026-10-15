"""Read-only eligibility check for one existing Tilda order.

This report never changes an order, document, journal, product or stock.
The existing /hatuna summary is accounting history, not an eligibility check.
"""
import os
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config.settings import settings
from app.database.session import async_session
from app.models.order import Order
from app.models.order_desk import OrderDesk
from app.models.fulfillment import ExternalOperation
from app.services.order_desk import authorized_manager, require_owner, DeskError
from app.services.order_source_service import source_plans
from app.services.warehouse_documents import plan_key
from app.services.hatuna_accounting import hatuna_operation_keys, provisional_cost, HatunaPreflightError
from app.services.product_service import ProductService
from app.services.telegram_display import telegram_rubles


def valid_uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value.lower()
    except (TypeError, ValueError, AttributeError):
        return False


def clean_warehouse(value):
    return "".join(str(value or "").casefold().split()).replace("x", "х")


def evaluate(snapshot, catalog, *, post_pick=False, auto_process=False,
             external_writes=False, environment="production"):
    """Pure assessment. Never infer physical receipt from provider stock."""
    blockers = []
    lines = [f"Проверка склада Х · заказ №{snapshot['order_id']}",
             f"Этап заказа: {snapshot['fulfillment_status']} · ревизия {snapshot['revision']}"]
    if snapshot["status"] == "rejected" or snapshot["needs_review"]:
        blockers.append("Заказ отменён либо требует проверки менеджера.")
    if snapshot["source"] != "website" or not snapshot["manual_fulfillment"]:
        blockers.append("Нужен подтверждённый клиентский заказ Tilda.")
    if snapshot["fulfillment_status"] != "assembled":
        blockers.append("Пока не подтверждена сборка; внутренний приход Х после сборки ещё невозможен.")
    if snapshot["delivery_method"] == "unselected":
        blockers.append("Не выбран способ доставки.")
    if not valid_uuid(snapshot["organization_id"]):
        blockers.append("Не подтверждён UUID организации МойСклад.")
    if not valid_uuid(snapshot["counterparty_id"]):
        blockers.append("Не привязан подтверждённый контрагент покупателя МойСклад.")
    if (environment == "production" and
            "test oms" in (snapshot["customer_name"] + " " + snapshot["comment"]).casefold()):
        blockers.append("Тестовый заказ TEST OMS: реальные документы МойСклад запрещены.")
    if not post_pick:
        blockers.append("Сценарий post-pick в Manager Bot выключен.")
    if not auto_process:
        blockers.append("Автоматический обработчик склада Х выключен.")
    if not external_writes:
        blockers.append("Внешние записи МойСклад отключены.")

    quantities = {id: 0 for id in snapshot["items"]}
    x_count = 0
    for plan in snapshot["plans"]:
        if plan["status"] not in {"planned", "exported"}:
            blockers.append(f"План №{plan['id']} не активен.")
        active = [a for a in plan["allocations"] if a["status"] != "cancelled"]
        source_kinds = {a["source_type"] for a in active}
        if len(source_kinds) > 1:
            blockers.append(f"План №{plan['id']} смешивает физические источники.")
        is_x = "partner_x" in source_kinds
        if is_x:
            x_count += 1
            key = hatuna_operation_keys(snapshot["order_id"], plan["id"])["receipt"]
            status = snapshot["operations"].get(key)
            sim = snapshot["operations"].get("simulation:" + key)
            lines.append(f"Склад Х · план №{plan['id']}: " +
                         ("документ не создан" if not status and not sim else
                          f"журнал={status or sim} (проверить документ)"))
            if status is not None or sim is not None:
                blockers.append(f"По плану Х №{plan['id']} уже есть операция. Сначала сверить журнал; повтор запрещён.")
        for a in active:
            item = snapshot["items"].get(a["item_id"])
            if item is None or a["qty"] <= 0:
                blockers.append("План содержит неизвестную позицию или количество.")
                continue
            quantities[a["item_id"]] += a["qty"]
            product = catalog.get(item["product_id"])
            if not valid_uuid(item["product_id"]) or not product:
                blockers.append(f"Позиция №{a['item_id']}: нет подтверждённой карточки товара МойСклад.")
                continue
            stores = [s for s in product.get("stocks", []) if s.get("id") == plan["warehouse_id"]]
            if len(stores) != 1:
                blockers.append(f"Позиция №{a['item_id']}: склад не найден у товара в МойСклад.")
                continue
            store = stores[0]
            store_is_x = clean_warehouse(store.get("name")) == "складх"
            if store_is_x != is_x:
                blockers.append(f"Позиция №{a['item_id']}: товар назначен не на тот физический склад.")
                continue
            if snapshot["warehouse_ids"] and plan["warehouse_id"] not in snapshot["warehouse_ids"]:
                blockers.append(f"План №{plan['id']}: склад не входит в список разрешённых.")
            if is_x:
                try:
                    cost = telegram_rubles(provisional_cost(item["sale_price_minor"]))
                except HatunaPreflightError:
                    cost = "не подтверждена"
                    blockers.append(f"Позиция №{a['item_id']}: невозможна предварительная оценка себестоимости Х.")
                stock = store.get("stock")
                reserve = store.get("reserve")
                lines.append(f"  {item['name'][:90]} · {a['qty']} шт. · расчётная стоимость {cost}/шт.")
                lines.append(f"  Учётный остаток Х по отчёту: {stock if stock is not None else 'неизвестен'}; " +
                             f"резерв: {reserve if reserve is not None else 'неизвестен'}")
                if stock is None or reserve is None:
                    blockers.append("Не подтверждены данные об учтённом остатке Х; требуется сверка.")
                elif stock > 0 or reserve > 0:
                    blockers.append("По товару Х уже есть учтённый остаток/резерв; нельзя делать повторный приход без сверки.")
                # A zero inventory indicator is not proof of physical receipt.
                lines.append("  Фактическое нахождение товара подтверждает только сборщик.")
            elif a["source_type"] == "own":
                available = store.get("available")
                if available is None or available < a["qty"]:
                    blockers.append(f"Позиция №{a['item_id']}: недостаточно подтверждённого свободного остатка своего склада.")
    if not x_count:
        blockers.append("В заказе отсутствует активный план склада Х.")
    if quantities != {id: i["qty"] for id, i in snapshot["items"].items()}:
        blockers.append("Распределение товаров по складам неполное или превышает заказ.")
    if snapshot["fulfillment_status"] == "new" and post_pick:
        lines.append("Сборка: разрешена только после физической проверки всех позиций "
                     "и полного распределения. Документы МойСклад этим не создаются.")
    elif snapshot["fulfillment_status"] == "new":
        lines.append("Сборка: сценарий после подтверждения наличия ещё не подключён для заказа.")
    lines.extend(["",
        "Реальные складские операции: " + ("ПОКА ЗАПРЕЩЕНЫ" if blockers else "локальные условия проверены"),
        *("• " + reason for reason in dict.fromkeys(blockers))])
    lines.append("Это проверка, НЕ создание/резервирование/отгрузка товара.")
    return "\n".join(lines)[:3900]


async def check_hatuna_order(order_id, actor, chat_id):
    """Authorized read-only DB snapshot, then fresh provider lookup, no lock or write."""
    if order_id <= 0:
        raise DeskError("Укажите положительный номер заказа.")
    async with async_session() as session:
        await authorized_manager(session, actor, chat_id)
        order = await session.scalar(
            select(Order).where(Order.id == order_id).options(selectinload(Order.items)))
        desk = await session.scalar(select(OrderDesk).where(OrderDesk.order_id == order_id))
        if not order or not desk or not order.manual_fulfillment:
            raise DeskError("Не найден подтверждённый заказ Tilda.")
        require_owner(desk, actor)
        plans = await source_plans(session, order_id)
        operations = {}
        plan_data = []
        for plan in plans:
            plan_data.append({
                "id": plan.id, "status": plan.status,
                "warehouse_id": plan.warehouse_id,
                "allocations": [
                    {"item_id": a.order_item_id, "qty": a.qty,
                     "source_type": a.source_type, "status": a.supply_status}
                    for a in plan.allocations],
            })
            if any(a.source_type == "partner_x" and a.supply_status != "cancelled"
                   for a in plan.allocations):
                key = hatuna_operation_keys(order_id, plan.id)["receipt"]
                for name in (key, "simulation:" + key):
                    recorded = await session.get(ExternalOperation, name)
                    if recorded:
                        operations[name] = recorded.status
        snapshot = {
            "order_id": order_id, "revision": order.revision,
            "status": order.status, "source": order.source,
            "manual_fulfillment": order.manual_fulfillment,
            "needs_review": order.needs_review,
            "customer_name": order.customer_name or "",
            "comment": order.comment or "",
            "fulfillment_status": order.fulfillment_status,
            "delivery_method": order.delivery_method,
            "counterparty_id": order.counterparty_id,
            "organization_id": settings.moysklad_organization_id,
            "warehouse_ids": settings.warehouse_ids,
            "items": {i.id: {"product_id": i.product_id, "name": i.name,
                             "qty": i.qty, "sale_price_minor": i.price}
                      for i in order.items},
            "plans": plan_data, "operations": operations,
        }
    try:
        catalog = {row["id"]: row for row in await ProductService().get_catalog_async(
            include_supply=False, fresh_stock=True)}
    except Exception:
        return (f"Проверка склада Х · заказ №{order_id}\n"
                "МойСклад сейчас недоступен: достоверная сверка товаров и остатков невозможна. "
                "Не запускайте оприходование.")
    from app.services.hatuna_post_pick import post_pick_enabled_for_order
    return evaluate(
        snapshot, catalog,
        post_pick=post_pick_enabled_for_order(order_id),
        auto_process=os.getenv("OMS_HATUNA_AUTO_PROCESS_ENABLED", "false").lower() == "true",
        external_writes=settings.external_writes_enabled,
        environment=settings.environment)
