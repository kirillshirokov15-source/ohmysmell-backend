import os
import asyncio
from dotenv import load_dotenv

from app.repositories.manager_repository import is_active_manager
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
from aiogram.filters import CommandStart
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

async def check_access(message: Message) -> bool:
    if not is_active_manager(message.from_user.id):
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

    await message.answer("📦 Новых заказов пока нет.")


@dp.message(F.text == "📋 Все заказы")
async def all_orders_handler(message: Message):
    if not await check_access(message):
        return

    await message.answer("📋 Список заказов пока пуст.")


@dp.message(F.text == "👥 Контрагенты")
async def contractors_handler(message: Message):
    if not await check_access(message):
        return

    await message.answer("👥 Контрагенты будут подключены после интеграции с МойСклад.")


@dp.message(F.text == "📦 Остатки")
async def stock_handler(message: Message):
    if not await check_access(message):
        return

    await message.answer("📦 Остатки будут доступны после подключения МойСклад.")


@dp.message(F.text == "⚙️ Настройки")
async def settings_handler(message: Message):
    if not await check_access(message):
        return

    await message.answer("⚙️ Настройки CRM пока не добавлены.")


@dp.callback_query(F.data.startswith("draft:"))
async def draft_callback_handler(callback: CallbackQuery):
    if not callback.from_user or not is_active_manager(callback.from_user.id):
        await safe_callback_answer(callback, "Нет доступа", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    try:
        action = parts[1]
        draft_id = int(parts[3] if action == "type" else parts[2])
        log_event(
            logger,
            "telegram_callback_received",
            callback_data=callback.data,
            telegram_user_id=callback.from_user.id,
            draft_id=draft_id,
        )
        if not await safe_callback_answer(callback):
            return
        log_event(
            logger,
            "telegram_callback_acknowledged",
            callback_data=callback.data,
            telegram_user_id=callback.from_user.id,
            draft_id=draft_id,
        )
        log_event(
            logger,
            "telegram_callback_action_started",
            action=action,
            draft_id=draft_id,
        )
        service = DraftOrderService()
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
                "telegram_callback_action_completed",
                action=action,
                draft_id=draft_id,
            )
            log_event(
                logger,
                "telegram_manager_action",
                action=action,
                draft_id=draft_id,
                order_id=order.id,
            )
            if callback.message:
                await callback.message.edit_text(
                    f"Черновик №{draft_id} подтверждён как заказ №{order.id}"
                )
            return
        else:
            logger.warning("Unknown Telegram draft callback action: %s", action)
            return
        log_event(
            logger,
            "telegram_callback_action_completed",
            action=action,
            draft_id=draft_id,
        )
        log_event(
            logger,
            "telegram_manager_action",
            action=action,
            draft_id=draft_id,
        )
        if draft is not None and callback.message:
            await callback.message.edit_text(
                build_draft_card(draft),
                reply_markup=InlineKeyboardMarkup.model_validate(
                    build_draft_keyboard(draft)
                ),
            )
    except (ValueError, IndexError, DraftOrderError) as error:
        logger.warning("Telegram callback rejected: %s", error)


async def main(*, drop_pending_updates: bool = False):
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    if drop_pending_updates:
        await bot.delete_webhook(drop_pending_updates=True)
        print("Pending Telegram updates: DROPPED")
        print("Telegram polling: STARTING")
    await dp.start_polling(bot)


def run_bot(*, drop_pending_updates: bool = False):
    asyncio.run(main(drop_pending_updates=drop_pending_updates))
