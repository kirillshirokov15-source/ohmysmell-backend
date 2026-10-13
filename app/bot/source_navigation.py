"""Actor-scoped source selection. Only the existing service can mutate an order.

Tokens are disposable navigation, never operation receipts. A restart simply
requires reopening the order; revisions and idempotency remain in the CRM.
"""
import secrets
import time
from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from app.database.session import async_session
from app.models.order import OrderItem
from app.models.supply import Supplier, SupplierOffer
from app.services.order_source_service import OrderSourceService
from app.services.order_desk import DeskError
from app.schemas.order_sources import SourceAction

navigation = {}


def source_warehouses(product, allowed_ids, source_type):
    """Distinct source and physical warehouse: X never appears under Own."""
    if source_type not in {"own", "partner_x", "external"}:
        return []
    def is_x(stock):
        name = (stock.get("name") or "").replace(" ", "").casefold()
        return name in {"складх", "складx"}
    return [stock for stock in product.get("stocks", [])
            if (not allowed_ids or stock["id"] in allowed_ids)
            and (is_x(stock) if source_type == "partner_x" else not is_x(stock))]


async def replace_screen(message, text, rows):
    """Edit one existing manager card; do not stack callback replies."""
    try:
        await message.edit_text(text[:3900], parse_mode=None,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).casefold():
            raise




def button(label, state):
    now = time.monotonic()
    for token in list(navigation):
        if navigation[token][0] < now:
            navigation.pop(token, None)
    token = secrets.token_hex(10)
    navigation[token] = (now + 600, state)
    return [{"text": label, "callback_data": "sourceui:" + token}]


async def show_sources(message, actor, order_id, *, edit=False):
    view = await OrderSourceService().read(order_id, actor, message.chat.id)
    from app.repositories.order_repository import get_order
    order = await get_order(order_id)
    editable = order.fulfillment_status == "new" and order.status != "rejected" and not order.needs_review
    from app.services.order_source_service import source_plans, simulation_allowed
    from app.services.warehouse_documents import frozen_plan
    async with async_session() as session:
        plans = await source_plans(session, order_id)
        frozen = any([await frozen_plan(session, plan) for plan in plans])
    state = dict(actor=actor, chat=message.chat.id, order_id=order_id, revision=view["revision"])
    rows, lines = [], [f"Обеспечение заказа №{order_id}"]
    for item in view["items"]:
        lines.append(f"{item['name']}: готово {item['ready']} из {item['qty']} шт.")
        remaining = item["qty"] - item["assigned"]
        if remaining > 0 and editable and not frozen:
            rows.append(button(f"Источник: {item['name'][:45]} · {remaining} шт.",
                dict(state, step="type", item_id=item["id"], qty=remaining)))
    for allocation in view["allocations"]:
        if allocation["status"] == "cancelled":
            continue
        label = {"own": "Свой склад", "partner_x": "X", "external": "Поставщик"}[allocation["source_type"]]
        lines.append(f"{label}: {allocation['qty']} шт. · " + ("готово" if allocation["ready"] else "ожидаем поступления"))
        if not editable or allocation["plan_status"] != "planned":
            continue
        if simulation_allowed() and allocation["status"] == "awaiting_receipt" and (
                allocation["source_type"] == "partner_x" or allocation["purchase_status"] == "received"):
            rows.append(button(f"Получено: {label} · {allocation['qty']} шт.", dict(state, step="apply",
                action="receive", allocation_id=allocation["id"])))
        if simulation_allowed() and allocation["status"] == "received":
            rows.append(button(f"Оприходовать: {label} (симуляция)", dict(state, step="apply",
                action="post_receipt", allocation_id=allocation["id"])))
        if not frozen:
            rows.append(button(f"Убрать источник: {label} · {allocation['qty']} шт.", dict(state, step="confirm_cancel",
                allocation_id=allocation["id"])))
    rows.append([{"text": "Назад к заказу", "callback_data": f"order:refresh:{order_id}:{view['revision']}"}])
    if edit:
        await replace_screen(message, "\n".join(lines), rows)
    else:
        await message.answer("\n".join(lines)[:3900], parse_mode=None,
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


def register(dp, check_access, safe_callback_answer):
    @dp.callback_query(F.data.startswith("sourceui:"))
    async def navigate(callback):
        if not await safe_callback_answer(callback):
            return
        message = callback.message.model_copy(update={"from_user": callback.from_user, "text": "/sources"}).as_(callback.bot)
        if not await check_access(message):
            return
        entry = navigation.get(callback.data.split(":", 1)[1])
        if not entry or entry[0] < time.monotonic():
            await replace_screen(callback.message,
                "Экран устарел. Откройте заказ снова из списка заказов.",
                [[{"text": "Открыть заказы", "callback_data": "orders:all:0"}]])
            return
        state = entry[1]
        if (state["actor"], state["chat"]) != (callback.from_user.id, message.chat.id):
            return
        try:
            service = OrderSourceService()
            view = await service.read(state["order_id"], state["actor"], state["chat"])
            if view["revision"] != state["revision"]:
                # Never replay an obsolete operation; refresh existing card.
                await show_sources(callback.message, state["actor"], state["order_id"], edit=True)
                return
            rows, text = [], "Выберите источник"
            empty_reason = None
            step = state["step"]
            if step == "type":
                for value, label in (("own", "Свой склад"), ("partner_x", "Склад Х"), ("external", "Внешний поставщик")):
                    rows.append(button(label, dict(state, step="x_warehouse" if value == "partner_x" else "warehouse", source_type=value)))
                rows.append(button(f"Количество: {state['qty']} шт. · изменить", dict(state,
                    step="quantity", quantity_next="type")))
            elif step == "x_warehouse":
                from app.services.product_service import ProductService
                from app.config.settings import settings
                async with async_session() as session:
                    item = await session.get(OrderItem, state["item_id"])
                if not item:
                    raise DeskError("Позиция заказа не найдена.")
                catalog = await ProductService().get_catalog_async(include_supply=False)
                product = next((p for p in catalog if p["id"] == item.product_id), {})
                warehouses = source_warehouses(product, settings.warehouse_ids, "partner_x")
                if len(warehouses) != 1:
                    empty_reason = ("Склад Х не найден однозначно среди разрешённых складов "
                                    "МойСклад. Проверьте его название и привязку товара.")
                else:
                    from app.bot.manager_inputs import prompt_input
                    state = dict(state, warehouse_id=warehouses[0]["id"],
                                 origin_message_id=callback.message.message_id)
                    await replace_screen(callback.message,
                        "Склад Х · заказ №" + str(state["order_id"])
                        + "\nВведите закупочную стоимость за штуку ответом на сообщение.",
                        [button("← Другой источник", dict(state, step="type"))])
                    await prompt_input(message, "source_cost", state,
                        "Введите закупочную цену склада Х за штуку в рублях.")
                    return
            elif step == "warehouse":
                from app.services.product_service import ProductService
                from app.config.settings import settings
                async with async_session() as session:
                    item = await session.get(OrderItem, state["item_id"])
                catalog = await ProductService().get_catalog_async(include_supply=False)
                product = next((p for p in catalog if p["id"] == item.product_id), {})
                # An X warehouse must never appear as an own-stock or external
                # receiving choice. One eligible receiving warehouse is selected
                # automatically: choosing a supply type must not ask for the same
                # warehouse again.
                warehouses = source_warehouses(product, settings.warehouse_ids, state["source_type"])
                if not warehouses:
                    empty_reason = ("Для этого источника не найден основной склад. "
                        "«Склад Х» не может быть выбран как собственный склад "
                        "или склад поступления внешнего поставщика.")
                elif len(warehouses) == 1:
                    warehouse = warehouses[0]
                    selected = dict(state, warehouse_id=warehouse["id"],
                                    warehouse_label=warehouse.get("name") or "Основной склад")
                    if state["source_type"] == "own":
                        text = (f"Заказ №{state['order_id']} · свой склад\n"
                            f"{selected['warehouse_label']} · {state['qty']} шт.\n"
                            "Подтвердить распределение? Реальные документы МойСклад этим не создаются.")
                        rows.append(button("✅ Подтвердить свой склад",
                                           dict(selected, step="apply", action="assign")))
                    else:
                        text = ("Склад для поступления: " + selected["warehouse_label"]
                            + ".\nСледующий шаг — выбрать предложение поставщика.")
                        rows.append(button("Выбрать поставщика",
                                           dict(selected, step="supplier")))
                    rows.append(button("← Другой источник", dict(state, step="type")))
                else:
                    text = "Выберите собственный склад / склад поступления"
                    for warehouse in warehouses:
                        rows.append(button(warehouse.get("name") or warehouse["id"], dict(state,
                            step="supplier" if state["source_type"] == "external" else "confirm",
                            warehouse_id=warehouse["id"],
                            warehouse_label=warehouse.get("name") or "Основной склад")))
            elif step == "supplier":
                text = "Выберите поставщика"
                async with async_session() as session:
                    if state["source_type"] == "partner_x":
                        # Legacy callback tokens also resolve to the fixed X partner.
                        rows.append(button("Продолжить: Склад Х", dict(state, step="cost")))
                    else:
                        item = await session.get(OrderItem, state["item_id"])
                        offers = (await session.execute(select(SupplierOffer, Supplier).join(Supplier).where(
                            SupplierOffer.product_id == item.product_id, SupplierOffer.active.is_(True),
                            SupplierOffer.availability != "unavailable", Supplier.status == "active",
                            Supplier.supplier_type == "external_wholesaler"))).all()
                        configuration_issues = set()
                        for offer, supplier in offers:
                            from app.models.buying import BuyingSupplier
                            buying_supplier = await session.get(BuyingSupplier, supplier.id)
                            if not buying_supplier:
                                configuration_issues.add("поставщик не зарегистрирован в Buying")
                            elif buying_supplier.currency != offer.currency_code:
                                configuration_issues.add("валюта в Buying не совпадает с предложением")
                            if not supplier.email:
                                configuration_issues.add("у поставщика не указан email")
                            if offer.availability_qty is not None and offer.availability_qty < state["qty"]:
                                configuration_issues.add("доступное количество меньше требуемого")
                            if (buying_supplier and buying_supplier.currency == offer.currency_code and supplier.email
                                    and (offer.availability_qty is None or offer.availability_qty >= state["qty"])):
                                rows.append(button(f"{supplier.name} · {offer.purchase_price_minor / 100:.2f} {offer.currency_code}",
                                    dict(state, step="confirm", offer_id=offer.id)))
                        if not rows:
                            empty_reason = (
                                "Для выбранного товара нет пригодного предложения внешнего поставщика. "
                                + ("Пока нет активных предложений, связанных с этим товаром."
                                   if not offers else "Что требуется проверить: " + "; ".join(sorted(configuration_issues)) + ".")
                                + " Настройка предложения в CRM/Buying обязательна, "
                                  "но заказ клиента можно оставить в ожидании закупки.")
            elif step == "cost":
                # A price is a deliberate manager input, never inferred from retail price.
                from app.bot.manager_inputs import prompt_input
                state = dict(state, origin_message_id=callback.message.message_id)
                await replace_screen(callback.message,
                    "Склад Х · заказ №" + str(state["order_id"])
                    + "\nВведите закупочную стоимость за штуку ответом на сообщение.",
                    [button("← Другой источник", dict(state, step="type"))])
                await prompt_input(message, "source_cost", state,
                    "Введите закупочную цену склада Х за штуку в рублях.")
                return
            elif step in {"confirm", "confirm_cancel"}:
                text = "Убрать это назначение источника?" if step == "confirm_cancel" else f"Назначить источник на {state['qty']} шт.?"
                rows.append(button("Подтвердить", dict(state, step="apply", action="cancel" if step == "confirm_cancel" else "assign")))
                if step == "confirm":
                    rows.append(button("Изменить количество", dict(state, step="quantity", quantity_next="apply")))
            elif step == "quantity":
                from app.bot.manager_inputs import prompt_input
                state = dict(state, origin_message_id=callback.message.message_id)
                await replace_screen(callback.message,
                    "Заказ №" + str(state["order_id"])
                    + "\nВведите количество ответом на сообщение.",
                    [button("← Другой источник", dict(state, step="type"))])
                await prompt_input(message, "source_quantity", state,
                                   "Сколько штук назначить этому источнику?")
                return
            elif step == "apply":
                action = state["action"]
                args = {"allocation_id": state["allocation_id"]} if action != "assign" else {
                    "parts": [{key: state[key] for key in ("item_id", "qty", "source_type", "warehouse_id",
                        "supplier_id", "base_cost_minor", "offer_id") if key in state}]}
                await service.act(state["order_id"], state["actor"], state["chat"], SourceAction(
                    action=action, expected_revision=state["revision"], idempotency_key=callback.data, **args))
                await show_sources(callback.message, state["actor"], state["order_id"], edit=True)
                return
            if not rows:
                text = empty_reason or "На этом шаге нет доступных вариантов. Проверьте настройки источника."
                if step in {"warehouse", "supplier", "x_warehouse"}:
                    rows.append(button("← Другой источник", dict(state, step="type")))
            rows.append([{"text": "Назад к заказу", "callback_data": f"order:refresh:{state['order_id']}:{state['revision']}"}])
            await replace_screen(callback.message, text, rows)
        except (DeskError, ValueError) as error:
            text = str(error) if isinstance(error, DeskError) else "Проверьте выбранные данные."
            await replace_screen(callback.message, "⚠️ " + text, [
                button("← Другой источник", dict(state, step="type")),
                [{"text": "Назад к заказу",
                  "callback_data": f"order:refresh:{state['order_id']}:0"}]])
