import os
import asyncio
from dotenv import load_dotenv

from app.repositories.manager_repository import is_active_manager
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    CallbackQuery,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.filters import CommandStart
from app.models.sales import CustomerType
from app.repositories.draft_order_repository import DraftOrderRepository
from app.services.draft_order_service import DraftOrderError, DraftOrderService

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
        await callback.answer("Нет доступа", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    try:
        action = parts[1]
        if action == "type":
            customer_type = CustomerType(parts[2])
            draft_id = int(parts[3])
            draft = await DraftOrderService().set_customer_type(
                draft_id, customer_type
            )
            await callback.answer(f"Тип клиента: {draft.customer_type}")
        elif action == "reject":
            draft_id = int(parts[2])
            await DraftOrderService().reject(draft_id)
            await callback.answer("Draft отклонён")
        elif action == "ambiguous":
            draft_id = int(parts[2])
            draft = await DraftOrderRepository().get(draft_id)
            if draft is None:
                raise DraftOrderError("Draft не найден")
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
            await callback.answer()
        else:
            await callback.answer("Неизвестное действие", show_alert=True)
    except (ValueError, IndexError, DraftOrderError) as error:
        await callback.answer(str(error), show_alert=True)


async def main():
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await dp.start_polling(bot)


def run_bot():
    asyncio.run(main())
