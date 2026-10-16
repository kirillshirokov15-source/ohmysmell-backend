"""No-write provider document plan for physically assembled Tilda own/X orders.

The preview assembles the same identifiers and cost rule as the live service,
but never invokes an external write adapter or inserts ledger rows.
"""
import os

from app.config.settings import settings
from app.services.hatuna_accounting import (
    HatunaItem, HatunaPreflightError, stock_entry_intent,
    hatuna_operation_keys, provisional_cost,
)
from app.services.hatuna_readonly_check import (
    load_hatuna_snapshot, valid_uuid, clean_warehouse,
)
from app.services.product_service import ProductService
from app.services.telegram_display import telegram_rubles


def build_document_plan(snapshot, catalog, *, post_pick=False, external_writes=False):
    order_id = snapshot["order_id"]
    lines = [
        f"План документов · заказ №{order_id} · БЕЗ ЗАПИСИ",
        f"Сборка: {snapshot['fulfillment_status']} · ревизия {snapshot['revision']}",
        "ЭТО ПРЕДПРОСМОТР, документы и резервы не создаются.",
        "",
    ]
    blockers = []
    if snapshot["fulfillment_status"] != "assembled":
        blockers.append("Сначала подтвердите физическую сборку.")
    if snapshot["status"] == "rejected" or snapshot["needs_review"]:
        blockers.append("Заказ отменён либо требует проверки менеджера.")
    if snapshot["source"] != "website" or not snapshot["manual_fulfillment"] or not post_pick:
        blockers.append("Нужен активный post-pick Tilda для этого заказа.")
    if not valid_uuid(snapshot["organization_id"]):
        blockers.append("Организация МойСклад не подтверждена.")
    if not valid_uuid(snapshot["counterparty_id"]):
        blockers.append("Нет привязанного покупателя МойСклад: резервы и отгрузки невозможны.")
    if snapshot["delivery_method"] == "unselected":
        blockers.append("Не выбран способ доставки.")
    if "test oms" in (snapshot["customer_name"] + " " + snapshot["comment"]).casefold():
        blockers.append("Заказ TEST OMS: реальная проводка запрещена.")
    if not external_writes:
        blockers.append("Внешние записи МойСклад отключены.")
    if os.getenv("OMS_HATUNA_AUTO_WRITES_ENABLED", "false").lower() != "true":
        blockers.append("Автоматические проводки Х выключены.")
    if os.getenv("OMS_HATUNA_AUTO_PROCESS_ENABLED", "false").lower() != "true":
        blockers.append("Автоматический складской обработчик выключен.")
    if not snapshot["plans"] or not snapshot["items"]:
        blockers.append("Нет полного товарного распределения.")
    requested = {item_id: item["qty"] for item_id, item in snapshot["items"].items()}
    assigned = {item_id: 0 for item_id in requested}
    x_plans = []
    for plan in snapshot["plans"]:
        if plan["status"] != "planned":
            blockers.append(f"План №{plan['id']} не в статусе planned.")
        active = [a for a in plan["allocations"] if a["status"] != "cancelled"]
        sources = {a["source_type"] for a in active}
        if not active or sources not in ({"own"}, {"partner_x"}):
            blockers.append(f"План №{plan['id']}: смешанный либо недопустимый источник.")
            continue
        is_x = sources == {"partner_x"}
        if is_x:
            x_plans.append(plan["id"])
        label = "склад Х" if is_x else "основной склад"
        lines.append(f"План №{plan['id']} · {label}")
        for a in active:
            item = snapshot["items"].get(a["item_id"])
            if not item or a["qty"] <= 0:
                blockers.append(f"План №{plan['id']}: товар или количество не определены.")
                continue
            assigned[a["item_id"]] += a["qty"]
            product = catalog.get(item["product_id"])
            if not valid_uuid(item["product_id"]) or not product or product.get("archived"):
                blockers.append(f"Товар {a['item_id']}: карточка МойСклад не подтверждена.")
                continue
            stores = [s for s in product.get("stocks", []) if s.get("id") == plan["warehouse_id"]]
            if len(stores) != 1 or (clean_warehouse(stores[0].get("name")) == "складх") != is_x:
                blockers.append(f"Товар {a['item_id']}: склад МойСклад не соответствует назначению.")
                continue
            stock = stores[0]
            if snapshot["warehouse_ids"] and plan["warehouse_id"] not in snapshot["warehouse_ids"]:
                blockers.append(f"План №{plan['id']}: склад не разрешён в настройках.")
            if is_x:
                try:
                    HatunaItem(item["product_id"], a["qty"], item["sale_price_minor"],
                               a["item_id"]).checked()
                    price = provisional_cost(item["sale_price_minor"])
                    lines.append(f"  {item['name'][:75]} · {a['qty']} шт. · приход {telegram_rubles(price)}/шт. (расчётно)")
                except HatunaPreflightError:
                    blockers.append(f"Товар {a['item_id']}: расчётная стоимость некорректна.")
                if stock.get("stock") is None or stock.get("reserve") is None:
                    blockers.append(f"Товар {a['item_id']}: остатки Х не подтверждены.")
                elif stock["stock"] > 0 or stock["reserve"] > 0:
                    blockers.append(f"Товар {a['item_id']}: уже есть учтённый остаток/резерв Х; повторный приход запрещён.")
            else:
                lines.append(f"  {item['name'][:75]} · {a['qty']} шт. · продажа {telegram_rubles(item['sale_price_minor'])}/шт.")
                available = stock.get("available")
                if available is None or available < a["qty"]:
                    blockers.append(f"Товар {a['item_id']}: собственного свободного остатка недостаточно.")
        if is_x:
            key = hatuna_operation_keys(order_id, plan["id"])["receipt"]
            lines.append("  1 × enter (внутреннее оприходование, без поставщика)")
            if snapshot["operations"].get(key) or snapshot["operations"].get("simulation:" + key):
                blockers.append(f"План №{plan['id']}: операция Х уже есть в журнале; требуется сверка.")
        for action in ("reserve", "ship", "release"):
            # Legacy/simulated operations must never silently be treated as new.
            key = f"warehouse:{plan['id']}:"
            if any((key in recorded and recorded.endswith(":" + action))
                   for recorded in snapshot["operations"]):
                blockers.append(f"План №{plan['id']}: найдена прежняя операция {action}; требуется сверка.")
        lines.append("  1 × customerorder (резерв с покупателем)")
        lines.append("  1 × demand (отгрузка после резерва)")
        lines.append("")
    if assigned != requested:
        blockers.append("Сумма количеств по складам не совпадает с заказом.")
    if not x_plans:
        blockers.append("Нет активного склада Х.")
    lines.append(f"Структура: {len(x_plans)} приход(ов) Х + {len(snapshot['plans'])} резерв(ов) + {len(snapshot['plans'])} отгрузок.")
    lines.append("Приход Х использует расчётную себестоимость, резервы/отгрузки – цену продажи.")
    lines.append("Отсутствие контрагента Хатуна не препятствует внутреннему enter; нужен покупатель для customerorder/demand.")
    lines.append("")
    lines.append("Реальное проведение: " + ("ЗАПРЕЩЕНО" if blockers else "условия предварительно выполнены"))
    lines.extend("• " + reason for reason in dict.fromkeys(blockers))
    lines.append("Нажатие кнопки не создаёт никаких документов или записей CRM.")
    return "\n".join(lines)[:3900]


async def preview_hatuna_document_plan(order_id, actor, chat_id):
    """Authorized local DB + fresh *read-only* provider stock. Zero writes."""
    snap = await load_hatuna_snapshot(order_id, actor, chat_id)
    try:
        catalogue = {
            row["id"]: row for row in await ProductService().get_catalog_async(
                include_supply=False, fresh_stock=True)
        }
    except Exception:
        return (f"План документов · заказ №{order_id}\n"
                "МойСклад не предоставил актуальные остатки. "
                "Предпросмотр блокирован; проводки не выполнялись.")
    from app.services.hatuna_post_pick import post_pick_enabled_for_order
    return build_document_plan(
        snap, catalogue,
        post_pick=post_pick_enabled_for_order(order_id),
        external_writes=settings.external_writes_enabled)
