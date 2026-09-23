"""Manager-only procurement UI. Registered only by the manager entrypoint."""
from aiogram import F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from app.database.session import async_session
from app.models.order import OrderItem
from app.models.supply import ProcurementRequest, SupplierOffer, Supplier, OrderItemSupply, XSettlement
from app.services.supply_service import ProcurementService, SupplyError, procurement_data, LABELS
from app.services.fx import FxError
from app.services.telegram_display import telegram_rubles

STATUS = {"needed": "Нужна закупка", "requested": "Запрос отправлен", "confirmed": "Подтверждена",
    "received": "Получена", "unavailable": "Недоступно", "cancelled": "Отменена"}


async def supply_order_card(order, shipments=()):
    from app.services.manager_workspace import order_card
    async with async_session() as session:
        rows = list((await session.execute(select(OrderItemSupply, XSettlement, ProcurementRequest, SupplierOffer, Supplier)
            .outerjoin(XSettlement, XSettlement.order_item_id == OrderItemSupply.order_item_id)
            .outerjoin(ProcurementRequest, ProcurementRequest.order_item_id == OrderItemSupply.order_item_id)
            .outerjoin(SupplierOffer, SupplierOffer.id == ProcurementRequest.offer_id)
            .outerjoin(Supplier, Supplier.id == SupplierOffer.supplier_id)
            .where(OrderItemSupply.order_id == order.id))).all())
        snapshots = [row[0] for row in rows]
        settlements = {row[1].order_item_id: row[1] for row in rows if row[1]}
        requests = {row[2].order_item_id: row[2] for row in rows if row[2]}
        selected = {row[0].order_item_id: (row[3], row[4]) for row in rows if row[3]}
    order.supply_external_only = bool(snapshots) and all(s.source_type == "external" for s in snapshots)
    order.supply_has_external = bool(requests)
    order.supply_received = bool(requests) and all(r.status == "received" for r in requests.values())
    order.supply_financial_review = any(s.status == "requires_financial_review" for s in settlements.values())
    lines = [order_card(order, shipments)]
    for s in snapshots:
        lines.append(f"Позиция #{s.order_item_id}: {LABELS[s.source_type]}")
        if s.supplier_name:
            lines.append(f"Поставщик: {s.supplier_name}")
        elif s.order_item_id in selected:
            offer, supplier = selected[s.order_item_id]
            lines.append(f"Поставщик: {supplier.name}; закупка за единицу: {offer.purchase_price_minor // 100}.{offer.purchase_price_minor % 100:02d} {offer.currency_code}")
            if offer.estimated_purchase_cost_rub_minor is not None:
                lines.append(f"Оценка за единицу: {telegram_rubles(offer.estimated_purchase_cost_rub_minor)} (ещё не зафиксирована)")
        if s.cost_snapshot:
            c = s.cost_snapshot
            lines.append(f"Закупка за единицу: {c['original_purchase_price_minor'] // 100}.{c['original_purchase_price_minor'] % 100:02d} {c['original_currency']}")
            lines.append(f"Фиксированная стоимость строки: {telegram_rubles(c['converted_purchase_cost_rub_minor'])}; продажа: {telegram_rubles(c['sale_price_minor'])}")
            if s.source_type == "external":
                lines.append(f"Наша маржа: {telegram_rubles(c['margin_rub_minor'])}")
        x = settlements.get(s.order_item_id)
        if x:
            if x.status == "requires_financial_review":
                lines.append("Требуется финансовая проверка: продажа ниже базовой стоимости X")
            else:
                lines.append(f"Наша маржа: {telegram_rubles(x.our_margin_minor)}; маржа X: {telegram_rubles(x.partner_margin_minor)}; к выплате X: {telegram_rubles(x.partner_due_minor)}")
        r = requests.get(s.order_item_id)
        if r:
            lines.append(f"Закупка #{r.id}: {STATUS[r.status]} · /procurement {order.id}")
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3850] + f"\nЗакупки: /procurement {order.id}"


async def show_procurement(message, identifier):
    async with async_session() as session:
        r = await session.get(ProcurementRequest, identifier)
        if not r:
            raise SupplyError("Закупка не найдена")
        item = await session.get(OrderItem, r.order_item_id)
        snapshot = await session.get(OrderItemSupply, r.order_item_id)
        offers = list((await session.scalars(select(SupplierOffer).where(SupplierOffer.product_id == item.product_id,
            SupplierOffer.active.is_(True)).order_by(SupplierOffer.id))).all())
        supplier_names = {s.id: s.name for s in (await session.scalars(select(Supplier))).all()}
    rows, lines = [], [f"Закупка #{r.id} · Заказ №{r.order_id}", f"{item.name[:200]} · {item.qty} шт.", STATUS[r.status]]
    if snapshot and snapshot.cost_snapshot:
        cost = snapshot.cost_snapshot
        lines.append(f"Зафиксировано: {telegram_rubles(cost['converted_purchase_cost_rub_minor'])} за строку; наша маржа: {telegram_rubles(cost['margin_rub_minor'])}")
        lines.append(f"Курс: {cost['fx_rate_to_rub']} ({cost['fx_source']}, {cost['fx_rate_date']})")
        lines.append("Текущие предложения ниже не изменяют зафиксированную стоимость.")
    def button(label, action, value=0):
        rows.append([{"text": label, "callback_data": f"proc:{action}:{r.id}:{r.revision}:{value}"}])
    for offer in offers:
        marker = "✓ " if offer.id == r.offer_id else ""
        lines.append(f"{marker}#{offer.id} {supplier_names[offer.supplier_id]}: {offer.purchase_price_minor // 100}.{offer.purchase_price_minor % 100:02d} {offer.currency_code}")
        if offer.estimated_purchase_cost_rub_minor is not None:
            lines.append(f"Оценка за единицу: {telegram_rubles(offer.estimated_purchase_cost_rub_minor)} ({offer.fx_source}, {offer.fx_rate_date})")
        if r.status == "needed" and offer.availability != "unavailable" and (offer.availability_qty is None or offer.availability_qty >= item.qty):
            button("Выбрать: " + supplier_names[offer.supplier_id][:35], "select", offer.id)
    if r.offer_id and r.status in {"needed", "requested"}:
        button("Обновить курс ЦБ", "estimate", r.offer_id)
        lines.append(f"Ручной курс: /fx {r.id} {r.revision} 90.50")
        if r.manual_fx_rate:
            lines.append(f"Ручной курс: {r.manual_fx_rate}")
        button("Запрос отправлен" if r.status == "needed" else "Поставщик подтвердил", "requested" if r.status == "needed" else "confirmed")
    if r.status == "confirmed":
        button("Товар получен", "received")
    if r.status in {"needed", "requested", "confirmed"}:
        button("Недоступно", "unavailable")
        button("Отменить закупку", "cancelled")
    button("Обновить", "view")
    await message.answer("\n".join(lines)[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def show_procurements(message, order_id=None):
    requests = await procurement_data(order_id)
    rows = [[{"text": f"Заказ №{r.order_id} · закупка #{r.id} · {STATUS[r.status]}",
        "callback_data": f"proc:view:{r.id}:{r.revision}:0"}] for r in requests]
    await message.answer("Требуют закупки — выберите позицию:" if rows else "Закупок нет.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


def register(dp, check_access, safe_callback_answer, manager_repository):
    @dp.message(Command("procurement"))
    @dp.message(F.text == "Требуют закупки")
    async def procurement_list(message):
        if not await check_access(message):
            return
        try:
            parts = message.text.split()
            order_id = int(parts[1]) if parts[0].startswith("/procurement") and len(parts) > 1 else None
            await show_procurements(message, order_id)
        except ValueError:
            await message.answer("Формат: /procurement или /procurement НОМЕР_ЗАКАЗА")

    @dp.message(Command("fx"))
    async def manual_fx(message):
        if not await check_access(message):
            return
        try:
            _, identifier, revision, rate = message.text.split()
            await ProcurementService().act(int(identifier), message.from_user.id, "fx", int(revision), manual_rate=rate)
            await show_procurement(message, int(identifier))
        except (SupplyError, FxError) as error:
            await message.answer(str(error))
        except ValueError:
            await message.answer("Формат: /fx НОМЕР_ЗАКУПКИ ВЕРСИЯ КУРС; пример: /fx 1 2 90.50")

    @dp.callback_query(F.data.startswith("proc:"))
    async def procurement_callback(callback):
        if not await safe_callback_answer(callback):
            return
        if not await manager_repository.is_active_by_telegram_id(callback.from_user.id):
            return
        try:
            _, action, identifier, revision, value = callback.data.split(":")
            identifier, revision, value = int(identifier), int(revision), int(value)
            service = ProcurementService()
            if action == "estimate":
                async with async_session() as session:
                    r = await session.get(ProcurementRequest, identifier)
                    if not r or r.revision != revision or r.offer_id != value or r.status not in {"needed", "requested"}:
                        raise SupplyError("Карточка устарела; обновите закупку")
                await service.refresh_estimate(value)
            elif action != "view":
                await service.act(identifier, callback.from_user.id, action, revision, offer_id=value if action == "select" else None)
            await show_procurement(callback.message, identifier)
        except (SupplyError, FxError) as error:
            await callback.message.answer(str(error))
        except ValueError:
            await callback.message.answer("Некорректное действие; обновите карточку")
