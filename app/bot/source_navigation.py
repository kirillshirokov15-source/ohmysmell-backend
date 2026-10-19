"""Actor-scoped source selection. Only the existing service can mutate an order.

Tokens are disposable navigation, never operation receipts. A restart simply
requires reopening the order; revisions and idempotency remain in the CRM.
"""
import secrets
import time
from aiogram import F
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from app.database.session import async_session
from app.models.order import OrderItem
from app.models.supply import Supplier, SupplierOffer
from app.services.order_source_service import OrderSourceService
from app.services.order_desk import DeskError
from app.schemas.order_sources import SourceAction

navigation = {}


async def refresh_order(message, order_id):
    from app.repositories.order_repository import get_order
    from app.services.manager_workspace import order_keyboard, shipments_for
    from app.bot.procurement import supply_order_card
    from app.services.telegram_cards import publish
    order = await get_order(order_id)
    plans = await shipments_for(order_id)
    entity = f"draft:{order.desk_draft_id}" if order.desk_draft_id else f"order:{order_id}"
    await publish(message.bot, message.chat.id, entity, await supply_order_card(order, plans),
        InlineKeyboardMarkup.model_validate(order_keyboard(order, plans)))


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
    from app.config.settings import settings
    documents_allowed = simulation_allowed() or (settings.environment == "production" and settings.external_writes_enabled)
    mode = "симуляция" if simulation_allowed() else "МойСклад"
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
        label = {"own": "Свой склад", "own_x": "Склад X (собственный)", "partner_x": "Партнёр X", "external": "Поставщик"}[allocation["source_type"]]
        lines.append(f"{label}: {allocation['qty']} шт. · " + ("готово" if allocation["ready"] else "ожидаем поступления"))
        if not editable or allocation["plan_status"] != "planned":
            continue
        if documents_allowed and allocation["status"] == "awaiting_receipt" and (
                allocation["source_type"] in {"own_x", "partner_x"} or allocation["purchase_status"] == "received"):
            rows.append(button(f"Получено: {label} · {allocation['qty']} шт.", dict(state, step="apply",
                action="receive", allocation_id=allocation["id"])))
        if documents_allowed and allocation["status"] == "received":
            rows.append(button(f"Оприходовать: {label} ({mode})", dict(state, step="document_confirm",
                document_action="receipt", target_id=allocation["id"])))
        if not frozen:
            rows.append(button(f"Убрать источник: {label} · {allocation['qty']} шт.", dict(state, step="confirm_cancel",
                allocation_id=allocation["id"])))
    if documents_allowed:
        from app.services.order_source_service import allocation_ready
        for plan in plans:
            active = [a for a in plan.allocations if a.supply_status != "cancelled"]
            if not active or plan.status == "cancelled":
                continue
            if order.fulfillment_status == "new" and all(allocation_ready(a, manual=True) for a in active):
                rows.append(button(f"Резерв склада {plan.warehouse_id} ({mode})", dict(state,
                    step="document_confirm", document_action="reserve", target_id=plan.id)))
            if order.fulfillment_status == "assembled":
                rows.append(button(f"Отгрузить со склада {plan.warehouse_id} ({mode})", dict(state,
                    step="document_confirm", document_action="ship", target_id=plan.id)))
    rows.append([{"text": "Назад к заказу", "callback_data": f"order:refresh:{order_id}:{view['revision']}"}])
    send = message.edit_text if edit else message.answer
    await send("\n".join(lines)[:3900], parse_mode=None, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    if edit:
        from app.services.telegram_cards import display_changed
        await display_changed(message.chat.id, message.message_id)


def register(dp, check_access, safe_callback_answer):
    @dp.callback_query(F.data.startswith("sourceui:"))
    async def navigate(callback):
        if not await safe_callback_answer(callback):
            return
        message = callback.message.model_copy(update={"from_user": callback.from_user, "text": "/sources"}).as_(callback.bot)
        if not await check_access(message):
            return
        from app.services.telegram_cards import obsolete, display_changed
        if await obsolete(message.chat.id, message.message_id):
            return
        entry = navigation.get(callback.data.split(":", 1)[1])
        if not entry or entry[0] < time.monotonic():
            await callback.message.answer("Откройте источники заказа заново.")
            return
        state = entry[1]
        if (state["actor"], state["chat"]) != (callback.from_user.id, message.chat.id):
            return
        try:
            service = OrderSourceService()
            view = await service.read(state["order_id"], state["actor"], state["chat"])
            if view["revision"] != state["revision"]:
                raise DeskError("Заказ изменился. Откройте источники заново.")
            rows, text = [], "Выберите источник"
            step = state["step"]
            if step == "type":
                for value, label in (("own", "Свой склад"), ("own_x", "Склад X (собственный)"), ("external", "Внешний поставщик")):
                    rows.append(button(label, dict(state, step="warehouse", source_type=value)))
                rows.append(button(f"Количество: {state['qty']} шт. · изменить", dict(state,
                    step="quantity", quantity_next="type")))
            elif step == "warehouse":
                from app.services.product_service import ProductService
                from app.config.settings import settings
                async with async_session() as session:
                    item = await session.get(OrderItem, state["item_id"])
                catalog = await ProductService().get_catalog_async(include_supply=False)
                product = next((p for p in catalog if p["id"] == item.product_id), {})
                text = "Выберите склад"
                for stock in product.get("stocks", []):
                    if settings.warehouse_ids and stock["id"] not in settings.warehouse_ids:
                        continue
                    rows.append(button(stock.get("name") or stock["id"], dict(state,
                        step="cost" if state["source_type"] == "own_x" else "supplier" if state["source_type"] != "own" else "confirm",
                        warehouse_id=stock["id"])))
            elif step == "supplier":
                text = "Выберите поставщика"
                async with async_session() as session:
                    if state["source_type"] == "partner_x":
                        suppliers = (await session.scalars(select(Supplier).where(
                            Supplier.supplier_type == "partner_x", Supplier.status == "active"))).all()
                        for supplier in suppliers:
                            rows.append(button(supplier.name, dict(state, step="cost", supplier_id=supplier.id)))
                    else:
                        item = await session.get(OrderItem, state["item_id"])
                        offers = (await session.execute(select(SupplierOffer, Supplier).join(Supplier).where(
                            SupplierOffer.product_id == item.product_id, SupplierOffer.active.is_(True),
                            SupplierOffer.availability != "unavailable", Supplier.status == "active",
                            Supplier.supplier_type == "external_wholesaler"))).all()
                        for offer, supplier in offers:
                            from app.models.buying import BuyingSupplier
                            buying_supplier = await session.get(BuyingSupplier, supplier.id)
                            if (buying_supplier and buying_supplier.currency == offer.currency_code and supplier.email
                                    and (offer.availability_qty is None or offer.availability_qty >= state["qty"])):
                                rows.append(button(f"{supplier.name} · {offer.purchase_price_minor / 100:.2f} {offer.currency_code}",
                                    dict(state, step="confirm", offer_id=offer.id)))
            elif step == "cost":
                # A price is a deliberate manager input, never inferred from retail price.
                from app.bot.manager_inputs import prompt_input
                await prompt_input(message, "source_cost", state, "Введите подтверждённую себестоимость за штуку в рублях.")
                return
            elif step in {"confirm", "confirm_cancel"}:
                text = "Убрать это назначение источника?" if step == "confirm_cancel" else f"Назначить источник на {state['qty']} шт.?"
                rows.append(button("Подтвердить", dict(state, step="apply", action="cancel" if step == "confirm_cancel" else "assign")))
                if step == "confirm":
                    rows.append(button("Изменить количество", dict(state, step="quantity", quantity_next="apply")))
            elif step == "quantity":
                from app.bot.manager_inputs import prompt_input
                await prompt_input(message, "source_quantity", state, "Сколько штук назначить этому источнику?")
                return
            elif step == "document_confirm":
                from app.services.order_source_service import simulation_allowed
                text = ("Симуляция" if simulation_allowed() else "Запись в МойСклад") + ": подтвердите складскую операцию."
                rows.append(button("Подтвердить операцию", dict(state, step="document_apply")))
                if state["document_action"] == "receipt":
                    rows.append(button("Указать себестоимость в рублях", dict(state, step="document_cost")))
            elif step == "document_cost":
                from app.bot.manager_inputs import prompt_input
                await prompt_input(message, "document_cost", state, "Введите фактическую себестоимость за штуку в рублях.")
                return
            elif step == "document_apply":
                from app.services.warehouse_documents import WarehouseDocumentService
                from app.integrations.moysklad.documents import FakeDocumentAdapter
                from app.services.order_source_service import simulation_allowed
                await WarehouseDocumentService(adapter=FakeDocumentAdapter() if simulation_allowed() else None).act(
                    state["order_id"], state["actor"], state["chat"], state["revision"], state["document_action"], state["target_id"],
                    receipt_cost_minor=state.get("receipt_cost_minor"))
                await refresh_order(callback.message, state["order_id"])
                return
            elif step == "apply":
                action = state["action"]
                args = {"allocation_id": state["allocation_id"]} if action != "assign" else {
                    "parts": [{key: state[key] for key in ("item_id", "qty", "source_type", "warehouse_id",
                        "supplier_id", "base_cost_minor", "offer_id") if key in state}]}
                await service.act(state["order_id"], state["actor"], state["chat"], SourceAction(
                    action=action, expected_revision=state["revision"], idempotency_key=callback.data, **args))
                await refresh_order(callback.message, state["order_id"])
                return
            if not rows:
                text = "Нет доступных вариантов. Проверьте склад или поставщика."
            rows.append([{"text": "Назад к заказу", "callback_data": f"order:refresh:{state['order_id']}:{state['revision']}"}])
            await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode=None)
            await display_changed(message.chat.id, message.message_id)
        except (DeskError, ValueError) as error:
            await callback.message.answer(str(error) if isinstance(error, DeskError) else "Проверьте выбранные данные.")
