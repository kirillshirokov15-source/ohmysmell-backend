import os
import asyncio
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart

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
        ],
    ],
    resize_keyboard=True
)


dp = Dispatcher()


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "🏠 Главное меню\n\n"
        "OhMySmell CRM запущена ✅\n\n"
        "Выберите раздел:",
        reply_markup=main_menu
    )


@dp.message(F.text == "📦 Новые заказы")
async def new_orders_handler(message: Message):
    await message.answer("📦 Новых заказов пока нет.")


@dp.message(F.text == "📋 Все заказы")
async def all_orders_handler(message: Message):
    await message.answer("📋 Список заказов пока пуст.")


@dp.message(F.text == "👥 Контрагенты")
async def contractors_handler(message: Message):
    await message.answer("👥 Контрагенты будут подключены после интеграции с МойСклад.")


@dp.message(F.text == "📦 Остатки")
async def stock_handler(message: Message):
    await message.answer("📦 Остатки будут доступны после подключения МойСклад.")


@dp.message(F.text == "⚙️ Настройки")
async def settings_handler(message: Message):
    await message.answer("⚙️ Настройки CRM пока не добавлены.")


async def main():
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await dp.start_polling(bot)


def run_bot():
    asyncio.run(main())