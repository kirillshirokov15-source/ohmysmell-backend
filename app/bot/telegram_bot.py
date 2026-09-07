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
        [
            KeyboardButton(text="📦 Новые заказы"),
            KeyboardButton(text="📋 Все заказы"),
        ],
        [
            KeyboardButton(text="👥 Контрагенты"),
            KeyboardButton(text="📦 Остатки"),
        ],
        [
            KeyboardButton(text="⚙️ Настройки"),
            KeyboardButton(text="🆔 Мой ID"),
        ],
    ],
    resize_keyboard=True
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
            callback_data=callback.data,
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

    drafts = await DraftOrderRepository().list(limit=10, active_only=True)
    if not drafts:
        await message.answer("Новых черновиков нет.")
    for draft in drafts:
        await message.answer(build_draft_card(draft), reply_markup=InlineKeyboardMarkup.model_validate(build_draft_keyboard(draft)))
    if len(drafts) == 10:
        await message.answer("Показаны 10 последних. /drafts 10 — следующая страница.")


@dp.message(F.text == "📋 Все заказы")
async def all_orders_handler(message: Message):
    if not await check_access(message):
        return

    from app.repositories.order_repository import list_orders
    from app.services.telegram_display import telegram_rubles
    orders = await list_orders(limit=20)
    await message.answer("\n".join(f"№{o.id} · {o.customer_name[:80]} · {telegram_rubles(o.total)} · {o.status}" for o in orders)[:4000] or "Заказов пока нет.")


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
        service = DraftOrderService()
        if parts[-1].startswith("v") and parts[-1][1:].isdigit():
            service.repository.expected_revision = int(parts[-1][1:])
        draft = None
        if action == "type":
            customer_type = CustomerType(parts[2])
            draft = await service.set_customer_type(draft_id, customer_type)
        elif action == "reject":
            draft = await service.reject(draft_id)
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
                draft_id=draft_id,
                order_id=order.id,
            )
            if callback.message:
                await safe_edit_message(
                    callback.message,
                    f"Черновик №{draft_id} подтверждён как заказ №{order.id}",
                    draft_id=draft_id,
                )
            outcome = "success"
            return
        else:
            logger.warning("Unknown Telegram draft callback action: %s", action)
            outcome = "unknown_action"
            return
        log_event(
            logger,
            "telegram_manager_action",
            action=action,
            draft_id=draft_id,
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
            await callback.message.answer("Действие не выполнено: обновите черновик через «Новые заказы». Проверьте тип клиента, товары и цены.")
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
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    if drop_pending_updates:
        await bot.delete_webhook(drop_pending_updates=True)
        print("Pending Telegram updates: DROPPED")
        print("Telegram polling: STARTING")
    await dp.start_polling(bot)


def run_bot(*, drop_pending_updates: bool = False):
    asyncio.run(main(drop_pending_updates=drop_pending_updates))
