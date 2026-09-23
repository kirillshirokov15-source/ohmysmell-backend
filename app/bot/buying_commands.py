"""Small command surfaces for shared-group mode and quantity corrections."""
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup


def register(dp, check_access, show_order_list):
    @dp.message(Command("procurements"))
    async def procurements(message):
        if await check_access(message):
            from app.bot.procurement import show_procurements
            await show_procurements(message)

    @dp.message(Command("orders"))
    async def orders(message):
        if await check_access(message):
            await show_order_list(message, "all", 0)

    @dp.message(Command("quantity"))
    async def quantity(message):
        if not await check_access(message):
            return
        from app.services.draft_order_service import DraftOrderService, DraftOrderError
        from app.services.order_lifecycle import InvalidOrderTransitionError
        from app.services.draft_telegram_service import build_draft_card, build_draft_keyboard
        try:
            _, draft, item, qty, revision = message.text.split()
            service = DraftOrderService()
            service.repository.expected_revision = int(revision)
            updated = await service.repository.correct_quantity(int(draft), int(item), int(qty), message.from_user.id)
            updated = await service.review(int(draft), updated)
            await message.answer(build_draft_card(updated), reply_markup=InlineKeyboardMarkup(**build_draft_keyboard(updated)))
        except (ValueError, DraftOrderError, InvalidOrderTransitionError):
            await message.answer("Проверьте карточку и команду: /quantity ЧЕРНОВИК ПОЗИЦИЯ КОЛИЧЕСТВО РЕВИЗИЯ")
