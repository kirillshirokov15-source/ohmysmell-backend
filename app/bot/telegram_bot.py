import os
import asyncio
from time import perf_counter
from dotenv import load_dotenv

from app.repositories.manager_repository import ManagerRepository
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.filters import CommandStart, Command
from app.services.order_lifecycle import InvalidOrderTransitionError
from app.models.sales import CustomerType
from app.repositories.draft_order_repository import DraftOrderRepository
from app.services.draft_order_service import DraftOrderError, DraftOrderService
from app.services.draft_telegram_service import (
    build_draft_card,
    build_draft_keyboard,
)
from app.services.telegram_display import customer_type_label
from app.logging_utils import log_event
import logging

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не найден в .env")


main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📦 Новые заказы"), KeyboardButton(text="📋 Все заказы")],
        [KeyboardButton(text="Требуют проверки"), KeyboardButton(text="Готовые заявки")],
        [KeyboardButton(text="В сборке"), KeyboardButton(text="Собранные")],
        [KeyboardButton(text="Отгруженные"), KeyboardButton(text="Отменённые")],
        [KeyboardButton(text="Неоплаченные"), KeyboardButton(text="Оплаченные")],
        [KeyboardButton(text="Поиск заказа"), KeyboardButton(text="📦 Остатки")],
        [KeyboardButton(text="👥 Контрагенты"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="🆔 Мой ID")],
        [KeyboardButton(text="Требуют закупки")],
    ], resize_keyboard=True,
)


dp = Dispatcher()
logger = logging.getLogger(__name__)
manager_repository = ManagerRepository()


def _duration_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 2)


def _is_stale_callback_error(error: TelegramBadRequest) -> bool:
    message = str(error).casefold()
    return "query is too old" in message or "query id is invalid" in message


async def safe_callback_answer(callback: CallbackQuery, *args, **kwargs) -> bool:
    try:
        await callback.answer(*args, **kwargs)
        return True
    except TelegramBadRequest as error:
        if not _is_stale_callback_error(error):
            raise
        log_event(
            logger,
            "telegram_callback_stale",
            action="ignored",
            action_source="callback",
            telegram_user_id=(
                callback.from_user.id if callback.from_user else None
            ),
        )
        return False


async def safe_edit_message(
    message: Message,
    text: str,
    *,
    draft_id: int,
    reply_markup=None,
) -> bool:
    log_event(
        logger,
        "telegram_card_refresh_started",
        draft_id=draft_id,
    )
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).casefold():
            raise
        log_event(
            logger,
            "telegram_card_not_modified",
            draft_id=draft_id,
        )
        return False
    log_event(
        logger,
        "telegram_card_refresh_completed",
        draft_id=draft_id,
    )
    return True

async def check_access(message: Message) -> bool:
    from app.bot.manager_group import allowed_event
    if not allowed_event(message):
        return False
    if not await manager_repository.is_active_by_telegram_id(
        message.from_user.id
    ):
        await message.answer("⛔ У вас нет доступа к OhMySmell CRM.")
        return False

    return True

@dp.message(CommandStart())
async def start_handler(message: Message):
    if not await check_access(message):
        return

    from app.bot.manager_group import group_id
    if group_id() is not None:
        await message.answer("Общий кабинет менеджеров: /drafts · /orders · /order НОМЕР · /procurements. Обычные сообщения игнорируются.")
        return

    await message.answer(
        "Обновляю меню…",
        reply_markup=ReplyKeyboardRemove(),
    )

    await message.answer(
        "🏠 Главное меню\n\n"
        "OhMySmell CRM запущена ✅\n\n"
        "Выберите раздел:",
        reply_markup=main_menu,
    )

@dp.message(F.text == "🆔 Мой ID")
async def my_id_handler(message: Message):
    if not await check_access(message):
        return

    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@dp.message(F.text == "📦 Новые заказы")
async def new_orders_handler(message: Message):
    if not await check_access(message):
        return

    await show_order_list(message, "new", 0)


@dp.message(F.text == "📋 Все заказы")
async def all_orders_handler(message: Message):
    if not await check_access(message):
        return

    await show_order_list(message, "all", 0)


@dp.message(F.text == "👥 Контрагенты")
async def contractors_handler(message: Message):
    if not await check_access(message):
        return

    from app.services.counterparty_matching_service import CounterpartyMatchingService
    candidates = await CounterpartyMatchingService().fallback_candidates_async()
    await message.answer("Последние контрагенты:\n" + "\n".join(str(c.get("name") or "Без имени")[:100] for c in candidates[:20]))


@dp.message(F.text == "📦 Остатки")
async def stock_handler(message: Message):
    if not await check_access(message):
        return

    from app.services.product_service import ProductService
    catalog = await ProductService().get_catalog_async()
    await message.answer("Остатки МойСклад (первые 20 товаров):\n" + "\n".join(f"{p['name'][:100]}: {p['total_available']} шт." for p in catalog[:20]))


@dp.message(F.text == "⚙️ Настройки")
async def settings_handler(message: Message):
    if not await check_access(message):
        return

    from app.config.settings import settings
    await message.answer("Настройки CRM\n"
        f"Среда: {settings.environment}\n"
        f"Внешние записи: {'включены' if settings.external_writes_enabled else 'выключены'}\n"
        f"Розничный прайс: {'настроен' if settings.moysklad_retail_price_type else 'требует настройки'}")


@dp.message(Command("drafts"))
async def drafts_page(message: Message):
    if not await check_access(message):
        return
    try:
        offset = max(int((message.text or "").split()[1]), 0)
    except (ValueError, IndexError):
        offset = 0
    drafts = await DraftOrderRepository().list(limit=10, offset=offset, active_only=True)
    for draft in drafts:
        await message.answer(build_draft_card(draft), reply_markup=InlineKeyboardMarkup.model_validate(build_draft_keyboard(draft)))
    await message.answer(f"Следующая страница: /drafts {offset + 10}" if len(drafts) == 10 else "Конец списка.")


@dp.message(Command("match"))
async def search_product_for_draft(message: Message):
    if not await check_access(message):
        return
    try:
        _, draft_id, item_id, query = (message.text or "").split(maxsplit=3)
        await message.answer("Ищу варианты товара…")
        draft = await DraftOrderService().search_item(int(draft_id), int(item_id), query)
        await message.answer(build_draft_card(draft), reply_markup=InlineKeyboardMarkup.model_validate(build_draft_keyboard(draft)))
    except (ValueError, DraftOrderError, InvalidOrderTransitionError):
        await message.answer("Формат: /match НОМЕР_ЧЕРНОВИКА НОМЕР_ПОЗИЦИИ название или артикул")


@dp.callback_query(F.data.startswith("draft:"))
async def draft_callback_handler(callback: CallbackQuery):
    handler_started_at = perf_counter()
    action_started_at = None
    action = None
    draft_id = None
    outcome = "error"
    telegram_user_id = callback.from_user.id if callback.from_user else None
    parts = (callback.data or "").split(":")
    try:
        action = parts[1]
        draft_id = int(parts[3] if action == "type" else parts[2])
        log_event(
            logger,
            "telegram_callback_received",
            telegram_user_id=telegram_user_id,
            draft_id=draft_id,
        )
        if not await safe_callback_answer(callback):
            outcome = "stale"
            return
        log_event(
            logger,
            "telegram_callback_acknowledged",
            telegram_user_id=telegram_user_id,
            draft_id=draft_id,
            duration_ms=_duration_ms(handler_started_at),
        )
        if not callback.from_user:
            outcome = "missing_user"
            return
        auth_started_at = perf_counter()
        authorized = await manager_repository.is_active_by_telegram_id(
            callback.from_user.id
        )
        log_event(
            logger,
            "telegram_manager_auth_completed",
            telegram_user_id=telegram_user_id,
            draft_id=draft_id,
            action=action,
            authorized=authorized,
            duration_ms=_duration_ms(auth_started_at),
            total_duration_ms=_duration_ms(handler_started_at),
        )
        if not authorized:
            outcome = "unauthorized"
            return
        action_started_at = perf_counter()
        log_event(
            logger,
            "telegram_callback_action_started",
            action=action,
            draft_id=draft_id,
        )
        if action not in {"refresh", "ambiguous"} and not (parts[-1].startswith("v") and parts[-1][1:].isdigit()):
            raise DraftOrderError("Карточка устарела; обновите черновик")
        service = DraftOrderService()
        if hasattr(service, "repository") and parts[-1].startswith("v") and parts[-1][1:].isdigit():
            service.repository.expected_revision = int(parts[-1][1:])
        draft = None
        if action == "refresh":
            draft = await DraftOrderRepository().get(draft_id)
        elif action == "type":
            customer_type = CustomerType(parts[2])
            draft = await service.set_customer_type(draft_id, customer_type)
        elif action == "reject":
            draft = await service.reject(draft_id)
        elif action == "qty":
            draft = await service.repository.correct_quantity(draft_id, int(parts[3]), int(parts[4]), callback.from_user.id)
            draft = await service.review(draft_id, draft)
        elif action == "qtymanual":
            if callback.message:
                await callback.message.answer(f"Введите: /quantity {draft_id} {int(parts[3])} КОЛИЧЕСТВО {int(parts[-1][1:])}")
            return
        elif action == "ambiguous":
            draft = await DraftOrderRepository().get(draft_id)
            if draft is None:
                raise DraftOrderError("Черновик не найден")
            ambiguous = [
                item for item in draft.items if item.match_status == "ambiguous"
            ]
            text = "\n\n".join(
                f"{item.raw_product_text}:\n"
                + "\n".join(
                    f"• {candidate.get('name')} "
                    f"({candidate.get('article') or 'без артикула'})"
                    for candidate in item.candidates
                )
                for item in ambiguous
            ) or "Неоднозначных позиций нет"
            text += f"\n\nПоиск товара: /match {draft_id} НОМЕР_ПОЗИЦИИ название или артикул"
            if callback.message:
                await callback.message.answer(text)
        elif action == "pick":
            current = await service._get_required(draft_id)
            item = next((i for i in current.items if i.id == int(parts[3])), None)
            candidate_index = int(parts[4])
            if not item or candidate_index < 0 or candidate_index >= len(item.candidates):
                raise DraftOrderError("Вариант товара устарел")
            draft = await service.resolve_product(draft_id, item.id, item.candidates[candidate_index]["id"])
        elif action == "cp":
            current = await service._get_required(draft_id)
            candidate_index = int(parts[3])
            if candidate_index < 0 or candidate_index >= len(current.counterparty_candidates):
                raise DraftOrderError("Вариант контрагента устарел")
            candidate = current.counterparty_candidates[candidate_index]
            draft = await service.link_counterparty(draft_id, candidate["id"], candidate.get("name") or candidate["id"])
        elif action == "product":
            item_id = int(parts[3])
            product_id = parts[4]
            draft = await service.resolve_product(draft_id, item_id, product_id)
        elif action == "counterparty":
            counterparty_id = parts[3]
            current = await DraftOrderRepository().get(draft_id)
            if current is None:
                raise DraftOrderError("Черновик не найден")
            candidate = next(
                (
                    item
                    for item in current.counterparty_candidates
                    if item.get("id") == counterparty_id
                ),
                None,
            )
            if candidate is None:
                raise DraftOrderError("Кандидат контрагента не найден")
            draft = await service.link_counterparty(
                draft_id,
                counterparty_id,
                candidate.get("name") or counterparty_id,
            )
        elif action == "counterparty_select":
            draft = await service.load_counterparty_candidates(draft_id)
        elif action == "finalize":
            order = await service.finalize(draft_id)
            log_event(
                logger,
                "telegram_manager_action",
                action=action,
                manager_id=telegram_user_id,
                draft_id=draft_id,
                order_id=order.id,
            )
            if callback.message:
                await safe_edit_message(
                    callback.message,
                    f"Черновик №{draft_id} подтверждён как заказ №{order.id}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[{ "text": "Открыть заказ", "callback_data": f"order:refresh:{order.id}:0"}]]),
                    draft_id=draft_id,
                )
            outcome = "success"
            return
        else:
            log_event(logger, "unknown_callback_action", result="rejected")
            outcome = "unknown_action"
            return
        log_event(
            logger,
            "telegram_manager_action",
            action=action,
            draft_id=draft_id,
            manager_id=telegram_user_id,
        )
        if draft is not None and callback.message:
            await safe_edit_message(
                callback.message,
                build_draft_card(draft),
                draft_id=draft_id,
                reply_markup=InlineKeyboardMarkup.model_validate(
                    build_draft_keyboard(draft)
                ),
            )
        outcome = "success"
    except (ValueError, IndexError, DraftOrderError, InvalidOrderTransitionError) as error:
        logger.warning("Telegram callback rejected error_type=%s", type(error).__name__)
        if callback.message:
            await callback.message.answer("Действие не выполнено: нажмите «Обновить» или откройте заявку через /draft НОМЕР. Проверьте тип клиента, товары и цены.")
        outcome = "rejected"
    except TelegramBadRequest:
        raise
    except Exception as error:
        logger.warning("Telegram callback failed error_type=%s", type(error).__name__)
        if callback.message:
            await callback.message.answer("Сервис временно недоступен. Повторите действие позже; подтверждение заказа защищено от дублирования.")
    finally:
        if action_started_at is not None:
            log_event(
                logger,
                "telegram_callback_action_completed",
                action=action,
                draft_id=draft_id,
                telegram_user_id=telegram_user_id,
                outcome=outcome,
                duration_ms=_duration_ms(action_started_at),
                total_duration_ms=_duration_ms(handler_started_at),
            )


async def main(*, drop_pending_updates: bool = False):
    from app.bot.runtime import run_worker
    await run_worker("manager", drop_pending_updates=drop_pending_updates)


def run_bot(*, drop_pending_updates: bool = False):
    asyncio.run(main(drop_pending_updates=drop_pending_updates))

from app.services.manager_workspace import order_card, order_keyboard, shipments_for, FULFILLMENT_LABELS
from app.services.order_operations import OrderOperations, OrderAction, OperationError
from app.repositories.order_repository import get_order, list_orders
from aiogram import BaseMiddleware


class ManagerRecoveryMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        started = perf_counter()
        message = event.message if isinstance(event, CallbackQuery) else event
        from app.bot.manager_group import allowed_event
        if not allowed_event(event, isinstance(event, CallbackQuery)):
            return
        try:
            return await handler(event, data)
        except Exception as error:
            log_event(logger, "manager_handler_failed", level=logging.ERROR, result=type(error).__name__)
            message = event.message if isinstance(event, CallbackQuery) else event
            if message and hasattr(message, "answer"):
                try:
                    await message.answer("Сервис временно недоступен. Обновите карточку перед повтором; выполненное действие сохранено.")
                except Exception:
                    pass
        finally:
            log_event(logger, "manager_handler_completed", action="callback" if isinstance(event, CallbackQuery) else "message",
                duration_ms=_duration_ms(started))


dp.message.outer_middleware(ManagerRecoveryMiddleware())
dp.callback_query.outer_middleware(ManagerRecoveryMiddleware())


async def show_order_list(message, category, offset):
    orders = await list_orders(limit=10, offset=offset, category=category)
    rows = [[{"text": f"№{o.id} · {FULFILLMENT_LABELS[o.fulfillment_status]} · {o.customer_name[:30]}",
              "callback_data": f"order:refresh:{o.id}:{o.revision}"}] for o in orders]
    if offset:
        rows.append([{"text": "Назад", "callback_data": f"orders:{category}:{max(0, offset-10)}"}])
    if len(orders) == 10:
        rows.append([{"text": "Далее", "callback_data": f"orders:{category}:{offset+10}"}])
    await message.answer("Заказы — выберите карточку:" if orders else "Заказов в разделе нет.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(F.text.in_({"Требуют проверки", "Готовые заявки", "В сборке", "Собранные", "Отгруженные", "Неоплаченные", "Оплаченные", "Отменённые", "Поиск заказа"}))
async def workspace_section(message):
    if not await check_access(message):
        return
    if message.text == "Поиск заказа":
        await message.answer("/order НОМЕР — заказ; /draft НОМЕР — заявка; /drafts — все активные заявки.")
    elif message.text in {"Требуют проверки", "Готовые заявки"}:
        status = "ready" if message.text == "Готовые заявки" else "needs_review"
        await show_draft_list(message, status, 0)
        if status == "needs_review":
            await show_order_list(message, "review", 0)
    else:
        category = {"В сборке": "assembling", "Собранные": "assembled", "Отгруженные": "shipped",
            "Неоплаченные": "unpaid", "Оплаченные": "paid", "Отменённые": "cancelled"}[message.text]
        await show_order_list(message, category, 0)


async def show_draft_list(message, status, offset):
    drafts = await DraftOrderRepository().list(limit=10, offset=offset, status=status)
    rows = [[{"text": f"Заявка №{d.id} · {(d.customer_name or 'Клиент')[:40]}",
        "callback_data": f"draft:refresh:{d.id}"}] for d in drafts]
    if offset:
        rows.append([{"text": "Назад", "callback_data": f"draftpage:{status}:{max(0,offset-10)}"}])
    if len(drafts) == 10:
        rows.append([{"text": "Далее", "callback_data": f"draftpage:{status}:{offset+10}"}])
    await message.answer("Заявки — выберите карточку:" if drafts else "Заявок в разделе нет.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("orders:"))
@dp.callback_query(F.data.startswith("draftpage:"))
async def workspace_page(callback):
    if not await safe_callback_answer(callback):
        return
    if not await manager_repository.is_active_by_telegram_id(callback.from_user.id):
        return
    kind, category, offset = callback.data.split(":")
    if kind == "orders":
        await show_order_list(callback.message, category, max(0, int(offset)))
    else:
        await show_draft_list(callback.message, category, max(0, int(offset)))


@dp.message(Command("order", "draft"))
async def find_order(message):
    if not await check_access(message):
        return
    try:
        command, identifier = message.text.split()
        identifier = int(identifier)
    except (ValueError, AttributeError):
        await message.answer("Формат: /order НОМЕР или /draft НОМЕР")
        return
    if command.split("@")[0] == "/draft":
        draft = await DraftOrderRepository().get(identifier)
        if not draft:
            await message.answer("Заявка не найдена.")
            return
        await message.answer(build_draft_card(draft), reply_markup=InlineKeyboardMarkup.model_validate(build_draft_keyboard(draft)))
    else:
        order = await get_order(identifier)
        if not order:
            await message.answer("Заказ не найден.")
            return
        plans = await shipments_for(order.id)
        await message.answer(await supply_order_card(order, plans), reply_markup=InlineKeyboardMarkup.model_validate(order_keyboard(order, plans)))


@dp.callback_query(F.data.startswith("order:"))
async def order_callback(callback):
    started = perf_counter()
    if not await safe_callback_answer(callback):
        return
    if not await manager_repository.is_active_by_telegram_id(callback.from_user.id):
        return
    try:
        _, action, identifier, version = callback.data.split(":")
        order_id, revision = int(identifier), int(version)
        order = await get_order(order_id)
        if not order:
            raise OperationError("Заказ не найден.")
        if action != "refresh" and order.revision != revision:
            raise OperationError("Карточка устарела. Нажмите «Обновить».")
        if action == "cancel_confirm":
            await callback.message.answer("Отменить этот неоплаченный заказ?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                {"text": "Да, отменить", "callback_data": f"order:cancel:{order_id}:{revision}"},
                {"text": "Оставить", "callback_data": f"order:refresh:{order_id}:{revision}"}]]))
            return
        if action == "allocate":
            from app.services.fulfillment_service import FulfillmentService
            await FulfillmentService().plan(order_id, expected_revision=revision)
        elif action == "procurement":
            await show_procurements(callback.message, order_id)
            return
        elif action != "refresh":
            extra = {}
            if action.startswith("delivery_"):
                extra["delivery_method"] = action.removeprefix("delivery_")
                action = "delivery"
            elif action == "delivered":
                extra["delivery_status"] = "delivered"
                action = "delivery"
            request = OrderAction(action=action, expected_revision=revision,
                idempotency_key=f"tg:{callback.from_user.id}:{callback.data}", **extra)
            order = await OrderOperations().act(order_id, callback.from_user.id, request)
        plans = await shipments_for(order_id)
        text = await supply_order_card(order, plans)
        keyboard = InlineKeyboardMarkup.model_validate(order_keyboard(order, plans))
        try:
            await safe_edit_message(callback.message, text, draft_id=0, reply_markup=keyboard)
        except TelegramBadRequest:
            await callback.message.answer(text, reply_markup=keyboard)
        log_event(logger, "manager_order_callback", order_id=order_id, manager_id=callback.from_user.id,
            action=action, result="success", duration_ms=_duration_ms(started))
    except (ValueError, OperationError) as error:
        await callback.message.answer(str(error) if isinstance(error, OperationError) else "Действие недоступно. Обновите карточку.")
    except Exception as error:
        from app.services.stock_allocation import StockAllocationError
        if isinstance(error, StockAllocationError):
            await callback.message.answer("Не удалось распределить заказ: проверьте товары и доступные остатки.")
        else:
            log_event(logger, "manager_order_callback", result=type(error).__name__, duration_ms=_duration_ms(started))
            await callback.message.answer("Сервис временно недоступен. Обновите карточку; сохранённые действия не дублируются.")

@dp.message(Command("payment", "tracking"))
async def operational_note(message):
    if not await check_access(message):
        return
    try:
        command, identifier, note = message.text.split(maxsplit=2)
        order = await get_order(int(identifier))
        if not order:
            raise OperationError("Заказ не найден.")
        args = {"action": "paid", "note": note} if command.split("@")[0] == "/payment" else {"action": "delivery", "delivery_reference": note}
        request = OrderAction(expected_revision=order.revision, idempotency_key=f"msg:{message.chat.id}:{message.message_id}", **args)
        updated = await OrderOperations().act(order.id, message.from_user.id, request)
        plans = await shipments_for(order.id)
        await message.answer(await supply_order_card(updated, plans), reply_markup=InlineKeyboardMarkup.model_validate(order_keyboard(updated, plans)))
    except (ValueError, OperationError) as error:
        await message.answer(str(error) if isinstance(error, OperationError) else "Формат: /payment НОМЕР примечание; /tracking НОМЕР номер_доставки")


@dp.message(Command("items"))
async def all_order_items(message):
    if not await check_access(message):
        return
    try:
        order = await get_order(int(message.text.split()[1]))
    except (ValueError, IndexError):
        order = None
    if not order:
        await message.answer("Формат: /items НОМЕР существующего заказа")
        return
    from app.services.telegram_display import telegram_rubles
    lines = [f"Заказ №{order.id} — все позиции:"]
    for item in order.items:
        lines.append(f"{item.name} · {item.qty} × {telegram_rubles(item.price)} = {telegram_rubles(item.item_total)}")
    for index, plan in enumerate(await shipments_for(order.id), 1):
        lines.append(f"Склад {index}: {plan.warehouse_id}")
        names = {item.id: item.name for item in order.items}
        lines.extend(f"{names[a.order_item_id]} · {a.qty} шт." for a in plan.allocations)
    chunk = ""
    for line in lines:
        if len(chunk)+len(line)+1 > 3900:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk)


from app.bot.procurement import register as register_procurement, supply_order_card, show_procurements
register_procurement(dp, check_access, safe_callback_answer, manager_repository)
from app.bot.buying_commands import register as register_buying_commands
register_buying_commands(dp, check_access, show_order_list)
