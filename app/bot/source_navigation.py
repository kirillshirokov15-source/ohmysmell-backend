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


def post_pick_enabled(order_id):
    from app.services.hatuna_post_pick import post_pick_enabled_for_order
    return post_pick_enabled_for_order(order_id)


def source_warehouses(product, allowed_ids, source_type):
    """Distinct source and physical warehouse: X never appears under Own."""
    if source_type not in {"own", "partner_x", "external"}:
        return []
    def is_x(stock):
        name = (stock.get("name") or "").replace(" ", "").casefold()
        return name in {"складх", "складx"}
    return [stock for stock in product.get("stocks", [])
            if stock.get("id") and not stock.get("archived", False)
            and (not allowed_ids or stock["id"] in allowed_ids)
            and (is_x(stock) if source_type == "partner_x" else not is_x(stock))]


async def replace_screen(message, text, rows):
    """Edit one existing manager card; do not stack callback replies."""
    try:
        await message.edit_text(text[:3900], parse_mode=None,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).casefold():
            raise




def x_cost_input(raw):
    """Parse rubles from an inline keypad without floating point or guessing."""
    from decimal import Decimal
    import re
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,10}(?:,[0-9]{1,2})?", raw):
        return None
    minor = Decimal(raw.replace(",", ".")) * 100
    if minor > 100_000_000_000:
        return None
    return int(minor)


def update_x_cost_digits(before, symbol):
    import re
    current = before if isinstance(before, str) else ""
    if symbol == "clear":
        return ""
    if symbol == "⌫":
        return current[:-1]
    if symbol == ",":
        return (current + ",") if current and "," not in current else ("0," if not current else current)
    if symbol not in "0123456789" or len(symbol) != 1:
        return current
    candidate = current + symbol
    return candidate if re.fullmatch(r"[0-9]{1,10}(?:,[0-9]{0,2})?", candidate) else current


def x_cost_keypad(state):
    """All price entry remains in one existing editable Telegram card."""
    entered = state.get("cost_digits", "")
    title = (f"Заказ №{state['order_id']} · себестоимость склада Х\n"
        "Розничная цена клиента из Tilda сохранена и не меняется.\n"
        "Укажите именно закупочную цену Х, только если она согласована.\n\n"
        f"Себестоимость за штуку: {entered or '—'} ₽")
    rows = []
    for digits in (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9"),
                   (",", "0", "⌫")):
        rows.append(sum((button(k, dict(state, step="cost_key", cost_key=k)) for k in digits), []))
    rows.append(button("Очистить", dict(state, step="cost_key", cost_key="clear")))
    if x_cost_input(entered) is not None:
        rows.append(button("Далее · проверить цену Х", dict(state, step="cost_review")))
    rows.append([{"text": "← К обеспечению", "callback_data":
        f"order:sources:{state['order_id']}:{state['revision']}"}])
    return title, rows


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
    from app.services.warehouse_documents import plan_document_read_models
    async with async_session() as session:
        plans = await source_plans(session, order_id)
        plan_docs = await plan_document_read_models(session, plans)
        frozen = any(row['frozen'] for row in plan_docs.values())
    state = dict(actor=actor, chat=message.chat.id, order_id=order_id, revision=view["revision"])
    rows, lines = [], [f"Обеспечение заказа №{order_id}"]
    for item in view["items"]:
        lines.append(f"{item['name']}: готово {item['ready']} из {item['qty']} шт.")
        remaining = item["qty"] - item["assigned"]
        if remaining > 0 and editable and not frozen:
            rows.append(button(f"Источник: {item['name'][:45]} · {remaining} шт.",
                dict(state, step="type", item_id=item["id"], qty=remaining)))
    import os
    post_pick = post_pick_enabled(order_id)
    for allocation in view["allocations"]:
        if allocation["status"] == "cancelled":
            continue
        label = {"own": "Свой склад", "partner_x": "X", "external": "Поставщик"}[allocation["source_type"]]
        lines.append(f"{label}: {allocation['qty']} шт. · " + ("готово" if allocation["ready"] else "ожидаем поступления"))
        if allocation["source_type"] == "partner_x":
            if post_pick:
                lines.append("  После «Заказ собран» автоматически: внутреннее оприходование "
                             "на склад Х → резерв → отгрузка.")
                lines.append("  Хатуна – пометка в документе, без заказа поставщику и уведомлений.")
                lines.append("  Учётная цена предварительная: цена сайта − 1 000 ₽/шт.")
            else:
                lines.append("  Этапы Х: получить товар → оприходовать в МойСклад → резерв → отгрузка.")
                rows.append(button("Проверить оприходование Х",
                    dict(state, step="x_receipt_preview", allocation_id=allocation["id"])))
                lines.append("  Наш физический склад Х — без контрагента и закупочной цены.")
                if (editable and not frozen and allocation["status"] == "awaiting_receipt"):
                    rows.append(button("Физически получено со склада Х",
                        dict(state, step="confirm_receive", allocation_id=allocation["id"])))
        if not editable or allocation["plan_status"] != "planned":
            continue
        if not post_pick and simulation_allowed() and allocation["status"] == "awaiting_receipt" and (
                allocation["source_type"] == "partner_x" or allocation["purchase_status"] == "received"):
            rows.append(button(f"Получено: {label} · {allocation['qty']} шт.", dict(state, step="apply",
                action="receive", allocation_id=allocation["id"])))
        if not post_pick and simulation_allowed() and allocation["status"] == "received":
            rows.append(button(f"Оприходовать: {label} (симуляция)", dict(state, step="apply",
                action="post_receipt", allocation_id=allocation["id"])))
        if not frozen:
            rows.append(button(f"Убрать источник: {label} · {allocation['qty']} шт.", dict(state, step="confirm_cancel",
                allocation_id=allocation["id"])))
    # Documents are ordered: real receipt → customer reserve → assembly → shipment.
    # Only local readiness is used for visibility; final confirmation rechecks
    # all prerequisites and invokes the idempotent external operation ledger.
    from app.services.order_source_service import allocation_ready
    for plan in plans:
        active_allocations = [a for a in plan.allocations if a.supply_status != "cancelled"]
        if not active_allocations:
            continue
        ready = all(allocation_ready(a, manual=order.manual_fulfillment)
                    for a in active_allocations)
        if (ready and plan.status == "planned" and not plan_docs[plan.id]["reserved"]
                and order.fulfillment_status == "new" and not order.needs_review):
            rows.append(button("Проверить резерв МойСклад",
                dict(state, step="plan_doc_preview", plan_id=plan.id, doc_action="reserve")))
        if (ready and plan.status == "planned" and plan_docs[plan.id]["reserved"]
                and not plan_docs[plan.id]["shipped"]
                and order.fulfillment_status == "assembled" and not order.needs_review):
            rows.append(button("Проверить отгрузку МойСклад",
                dict(state, step="plan_doc_preview", plan_id=plan.id, doc_action="ship")))
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
            if post_pick_enabled(state["order_id"]) and state.get("source_type") == "external":
                raise DeskError("Внешние закупки работают в Buying, не в заказах Tilda.")
            rows, text = [], "Выберите источник"
            empty_reason = None
            step = state["step"]
            if step == "type":
                allowed = (("own", "Свой склад"), ("partner_x", "Склад Х")) if post_pick_enabled(state["order_id"]) else (
                    ("own", "Свой склад"), ("partner_x", "Склад Х"), ("external", "Внешний поставщик"))
                for value, label in allowed:
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
                # Navigation needs only warehouse identities. Any actual
                # assignment still uses the fresh catalog in OrderSourceService.
                stores = await ProductService().async_gateway.get_stores()
                warehouses = source_warehouses({"stocks": stores}, settings.warehouse_ids, "partner_x")
                if len(warehouses) != 1:
                    empty_reason = ("Склад Х не найден однозначно среди разрешённых складов "
                                    "МойСклад. Проверьте его название и привязку товара.")
                else:
                    # The Tilda retail price is immutable. The provisional X
                    # valuation is an explicit business rule, not a supplier debt.
                    item_view = next((row for row in view["items"]
                        if row["id"] == state["item_id"]), None)
                    if not item_view:
                        raise DeskError("Позиция заказа не найдена.")
                    state = dict(state, warehouse_id=warehouses[0]["id"])
                    from app.services.telegram_display import telegram_rubles
                    if post_pick_enabled(state["order_id"]):
                        from app.services.hatuna_accounting import provisional_cost, HatunaPreflightError
                        try:
                            value = telegram_rubles(provisional_cost(item_view['sale_unit_minor']))
                        except HatunaPreflightError:
                            value = "нужна ручная проверка (цена продажи ≤ 1 000 ₽)"
                        text = (f"Склад Х · заказ №{state['order_id']}\n"
                            f"Количество: {state['qty']} шт.\n"
                            f"Продажа Tilda: {telegram_rubles(item_view['sale_unit_minor'])} за шт.\n"
                            f"Предварительная учётная цена: {value} за шт.\n"
                            "Оприходование после подтверждения сборки; без заказа поставщику.\n"
                            "Назначить склад Х?")
                    else:
                        text = (f"Склад Х · заказ №{state['order_id']}\n"
                            f"Количество: {state['qty']} шт.\n"
                            f"Цена продажи по Tilda: {telegram_rubles(item_view['sale_unit_minor'])} за шт.\n"
                            "Закупочная себестоимость Х: не указана (вносится отдельно).\n"
                            "Назначить склад Х без закупки, резерва или финансового расчёта?")
                    rows.append(button("✅ Назначить склад Х",
                                       dict(state, step="apply", action="assign")))
                    rows.append(button("← Другой источник", dict(state, step="type")))
            elif step == "warehouse":
                from app.services.product_service import ProductService
                from app.config.settings import settings
                async with async_session() as session:
                    item = await session.get(OrderItem, state["item_id"])
                # Navigation only; source assignment checks product and
                # fresh stock independently before any inventory claim.
                stores = await ProductService().async_gateway.get_stores()
                warehouses = source_warehouses({"stocks": stores}, settings.warehouse_ids,
                                               state["source_type"])
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
            elif step == "x_receipt_preview":
                from app.services.x_warehouse_preview import x_receipt_preflight
                outcome = await x_receipt_preflight(state["order_id"], state["allocation_id"],
                    state["actor"], state["chat"])
                text = outcome["text"]
                if not outcome["physically_received"] and not outcome["already_posted"]:
                    rows.append(button("1. Подтвердить физическое получение",
                        dict(state, step="confirm_receive")))
                if outcome["ready"]:
                    rows.append(button("⚠️ Реальное оприходование в МойСклад",
                        dict(state, step="x_receipt_confirm")))
                rows.append(button("← К обеспечению", dict(state, step="source_home")))
            elif step == "x_receipt_find_partner":
                from app.services.x_warehouse_preview import lookup_x_counterparty
                partner = await lookup_x_counterparty()
                state = dict(state, partner_uuid=partner["id"])
                text = (f"Контрагент «Склад Х» найден в МойСклад.\n"
                    f"Название: {partner['name']}\n"
                    f"UUID: {partner['id']}\n\n"
                    "Это существующий контрагент. После подтверждения он будет "
                    "привязан к единственной локальной карточке Х в CRM. "
                    "Новая карточка МойСклад НЕ создаётся.")
                rows.append(button("✅ Привязать контрагента Х",
                    dict(state, step="x_receipt_bind_partner")))
                rows.append(button("Отмена", dict(state, step="x_receipt_preview")))
            elif step == "x_receipt_bind_partner":
                from app.services.x_warehouse_preview import bind_x_counterparty
                result = await bind_x_counterparty(state["order_id"],
                    state["allocation_id"], state["actor"], state["chat"],
                    state["partner_uuid"])
                text = ("Контрагент «Склад Х» успешно привязан к МойСклад."
                    if result == "linked" else
                    "Контрагент Х уже был корректно привязан.")
                rows.append(button("← Проверка оприходования Х",
                    dict(state, step="x_receipt_preview")))
            elif step == "x_receipt_confirm":
                from app.services.x_warehouse_preview import x_receipt_preflight
                outcome = await x_receipt_preflight(state["order_id"], state["allocation_id"],
                    state["actor"], state["chat"])
                if not outcome["ready"]:
                    raise DeskError("Условия прихода изменились. Проверьте оприходование заново.")
                from app.services.telegram_display import telegram_rubles
                text = (f"РЕАЛЬНОЕ оприходование МойСклад · заказ №{state['order_id']}\n"
                    f"Товар: {outcome['product_name'][:140]}\n"
                    f"Количество: {outcome['quantity']} шт.\n"
                    f"Склад: Х\n"
                    "Склад Х — наш склад, поставщик не нужен.\n"
                    "Будет создан документ «Оприходование» (enter).\n"
                    "Цена продажи из Tilda не подменяет учётную стоимость.\n"
                    "При подтверждении будет создан настоящий документ оприходования. "
                    "При неизвестном результате повтор запрещён — нужна сверка.")
                rows.append(button("✅ Подтверждаю реальное оприходование",
                                   dict(state, step="execute_live_receipt")))
                rows.append(button("Отменить", dict(state, step="x_receipt_preview")))
            elif step == "execute_live_receipt":
                from app.services.x_warehouse_preview import x_receipt_preflight
                outcome = await x_receipt_preflight(state["order_id"], state["allocation_id"],
                    state["actor"], state["chat"])
                if not outcome["ready"] or outcome["order_revision"] != state["revision"]:
                    raise DeskError("Данные поступления изменились. Откройте проверку заново.")
                from app.services.warehouse_documents import WarehouseDocumentService
                from app.services.external_operation_service import ExternalOperationConflict
                try:
                    posted = await WarehouseDocumentService().act(
                        state["order_id"], state["actor"], state["chat"],
                        state["revision"], "receipt", state["allocation_id"])
                except (DeskError, ExternalOperationConflict) as error:
                    raise DeskError(str(error)) from error
                except Exception:
                    # External create can have succeeded despite a timeout.
                    # Do not retry or present a fake "failed" confirmation.
                    import logging
                    logging.getLogger(__name__).exception("x_receipt_outcome_uncertain")
                    await replace_screen(callback.message,
                        "Неизвестен результат оприходования. НЕ повторяйте операцию. "
                        "Проверьте журнал внешних операций и документ МойСклад.",
                        [[{"text": "К заказу", "callback_data":
                            f"order:refresh:{state['order_id']}:0"}]])
                    return
                await replace_screen(callback.message,
                    f"Оприходование подтверждено МойСклад. Документ {posted['id']}. "
                    "Далее нужно создать резерв, собрать и отгрузить товар.",
                    [[{"text": "Обеспечение заказа",
                       "callback_data": f"order:sources:{state['order_id']}:{posted['revision']}"}]])
                return
            elif step == "plan_doc_preview":
                from app.services.x_warehouse_preview import plan_document_preflight
                report = await plan_document_preflight(
                    state["order_id"], state["plan_id"], state["doc_action"],
                    state["actor"], state["chat"])
                text = report["text"]
                if report["ready"]:
                    rows.append(button("⚠️ Продолжить к реальной операции",
                        dict(state, step="plan_doc_confirm")))
                rows.append(button("← К обеспечению", dict(state, step="source_home")))
            elif step == "plan_doc_confirm":
                from app.services.x_warehouse_preview import plan_document_preflight
                report = await plan_document_preflight(
                    state["order_id"], state["plan_id"], state["doc_action"],
                    state["actor"], state["chat"])
                if not report["ready"]:
                    raise DeskError("Данные резерва/отгрузки изменились. Выполните проверку заново.")
                verb = "РЕЗЕРВ" if state["doc_action"] == "reserve" else "ОТГРУЗКУ"
                text = (f"РЕАЛЬНАЯ ОПЕРАЦИЯ МойСклад · заказ №{state['order_id']}\n"
                    f"Будет создан документ: {verb}.\n"
                    "Это изменит складской учёт. Если результат неизвестен, не повторяйте команду.")
                rows.append(button("✅ Создать реальный документ",
                    dict(state, step="plan_doc_execute")))
                rows.append(button("Отмена", dict(state, step="source_home")))
            elif step == "plan_doc_execute":
                from app.services.x_warehouse_preview import plan_document_preflight
                report = await plan_document_preflight(
                    state["order_id"], state["plan_id"], state["doc_action"],
                    state["actor"], state["chat"])
                if not report["ready"] or report["revision"] != state["revision"]:
                    raise DeskError("Складской план устарел. Начните проверку заново.")
                from app.services.warehouse_documents import WarehouseDocumentService
                try:
                    record = await WarehouseDocumentService().act(
                        state["order_id"], state["actor"], state["chat"],
                        state["revision"], state["doc_action"], state["plan_id"])
                except DeskError as error:
                    raise DeskError(str(error)) from error
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception("warehouse_plan_outcome_uncertain")
                    await replace_screen(callback.message,
                        "Результат МойСклад неизвестен. Не повторяйте операцию; "
                        "проверьте журнал и документ в МойСклад.",
                        [[{"text": "К заказу", "callback_data":
                            f"order:refresh:{state['order_id']}:0"}]])
                    return
                await replace_screen(callback.message,
                    f"МойСклад подтвердил документ: {record['id']}. "
                    + ("Теперь можно начать сборку." if state["doc_action"] == "reserve"
                       else "Отгрузка создана. Проверьте доставку и оплату."),
                    [[{"text": "К заказу", "callback_data":
                        f"order:refresh:{state['order_id']}:{record['revision']}"}]])
                return
            elif step == "confirm_receive":
                text = ("Подтвердить, что товар со склада Х фактически получен? "
                    "Это отмечает только получение в CRM и НЕ создаёт документ МойСклад.")
                rows.append(button("✅ Товар физически получен",
                                   dict(state, step="apply", action="receive")))
                rows.append(button("Отмена", dict(state, step="source_home")))
            elif step == "source_home":
                await show_sources(callback.message, state["actor"], state["order_id"], edit=True)
                return
            elif step == "cost":
                # The optional X cost does not require ForceReply or new chat messages.
                if "allocation_id" not in state:
                    await show_sources(callback.message, state["actor"], state["order_id"], edit=True)
                    return
                state = dict(state, cost_digits="")
                text, rows = x_cost_keypad(state)
            elif step == "cost_key":
                state = dict(state, cost_digits=update_x_cost_digits(
                    state.get("cost_digits", ""), state.get("cost_key", "")))
                state.pop("cost_key", None)
                text, rows = x_cost_keypad(state)
            elif step == "cost_review":
                minor = x_cost_input(state.get("cost_digits"))
                if minor is None or "allocation_id" not in state:
                    raise DeskError("Укажите корректную себестоимость.")
                from app.services.telegram_display import telegram_rubles
                text = (f"Заказ №{state['order_id']} · себестоимость Х\n"
                    f"Закупочная цена: {telegram_rubles(minor)} за штуку.\n"
                    "Розничная цена клиента из Tilda остаётся неизменной.\n"
                    "Подтвердите только реальную согласованную цену склада Х.")
                rows.append(button("✅ Подтвердить себестоимость",
                    dict(state, x_unit_cost_minor=minor, step="apply", action="set_x_cost")))
                rows.append(button("← Исправить цену",
                    dict(state, step="cost_key", cost_key="⌫")))
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
                if action == "set_x_cost":
                    args["x_unit_cost_minor"] = state["x_unit_cost_minor"]
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
