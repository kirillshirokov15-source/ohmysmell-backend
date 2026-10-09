"""Explicit group commands only. Ordinary group text never becomes a customer reply."""
from aiogram import F
from aiogram.types import InlineKeyboardMarkup
from aiogram.filters import Command
from app.bot.manager_group import allowed_event
from app.services.order_desk import DeskService, DeskError, authorized_manager, audit, now
from app.services.desk_display import desk_card


async def desk_access(actor, chat_id):
    from app.database.session import async_session
    try:
        async with async_session() as session:
            await authorized_manager(session, actor, chat_id)
    except DeskError:
        return False
    return True


def register(dp, check_access, safe_callback_answer):
    @dp.message(Command("site", "siteitems", "sitereprice", "reply", "outbox_retry", "outbox_sent"))
    async def desk_command(message):
        if not await check_access(message):
            return
        if not await desk_access(message.from_user.id, message.chat.id):
            return
        try:
            parts = message.text.split(maxsplit=2)
            action, identifier = parts[0].split("@")[0], int(parts[1])
            service = DeskService()
            if action == "/siteitems":
                from app.services.desk_display import desk_items
                lines = await desk_items(identifier)
                chunk = ""
                for line in lines:
                    if len((chunk + line).encode("utf-16-le")) > 7500:
                        await message.answer(chunk, parse_mode=None, protect_content=True)
                        chunk = ""
                    chunk += line + "\n"
                if chunk:
                    await message.answer(chunk, parse_mode=None, protect_content=True)
                return
            if action == "/reply":
                await service.reply(identifier, message.from_user.id, message.chat.id, message.message_id, parts[2])
                await message.answer(f"Ответ по заявке №{identifier} поставлен в очередь.", parse_mode=None)
                return
            if action in {"/outbox_retry", "/outbox_sent"}:
                from app.database.session import async_session
                from app.models.order_desk import DeskMessage, OrderDesk
                from app.services.order_desk import require_owner
                async with async_session() as session, session.begin():
                    await authorized_manager(session, message.from_user.id, message.chat.id)
                    task = await session.get(DeskMessage, identifier, with_for_update=True)
                    if not task or not task.draft_id or task.status not in {"failed", "blocked", "uncertain"}:
                        raise DeskError("Сообщение не требует восстановления.")
                    desk = await session.get(OrderDesk, task.draft_id)
                    require_owner(desk, message.from_user.id)
                    from app.services.desk_delivery_policy import DeskDeliveryPolicy
                    role = "client" if task.direction == "to_customer" else "manager"
                    if not DeskDeliveryPolicy.load(role).permits(task):
                        raise DeskError("Сообщение не включено в разрешённую staging-проверку.")
                    if action == "/outbox_sent":
                        sent_id = int(parts[2])
                        if sent_id <= 0:
                            raise DeskError("Нужен Telegram message ID.")
                        task.status, task.telegram_message_id, task.sent_at = "sent", sent_id, now()
                    else:
                        task.status, task.available_at = "pending", now()
                    audit(session, desk, "delivery_reconciled", message.from_user.id,
                        message_id=identifier, result=task.status)
                await message.answer("Состояние доставки сохранено.")
                return
            if action == "/sitereprice":
                await service.reprice(identifier, message.from_user.id, message.chat.id)
            text, keyboard = await desk_card(identifier)
            await message.answer(text, reply_markup=keyboard, parse_mode=None)
        except (IndexError, ValueError) as error:
            await message.answer(str(error) if isinstance(error, DeskError) else
                "Формат: /site ЗАЯВКА; /reply ЗАЯВКА текст; /sitereprice ЗАЯВКА; /outbox_retry ID; /outbox_sent ID TELEGRAM_MESSAGE_ID", parse_mode=None)

    @dp.callback_query(F.data.startswith("desk:"))
    async def desk_callback(callback):
        if not allowed_event(callback, callback=True):
            return
        from app.repositories.manager_repository import ManagerRepository
        if not await ManagerRepository().is_active_by_telegram_id(callback.from_user.id):
            return
        if not await desk_access(callback.from_user.id, callback.message.chat.id):
            return
        if not await safe_callback_answer(callback):
            return
        try:
            parts = callback.data.split(":")
            action, draft_id = parts[1], int(parts[2])
            service = DeskService()
            if action == "claim":
                await service.claim(draft_id, callback.from_user.id, callback.message.chat.id)
            elif action == "reply":
                await callback.message.answer(f"Ответ только через явную команду: /reply {draft_id} текст\nПроверьте номер заявки перед отправкой.")
                return
            elif action == "history":
                from app.services.desk_display import desk_history
                await callback.message.answer(await desk_history(draft_id), parse_mode=None, protect_content=True)
                return
            elif action in {"wait", "payment"}:
                await service.stage(draft_id, callback.from_user.id, callback.message.chat.id,
                    "awaiting_confirmation" if action == "wait" else "awaiting_payment")
            elif action == "confirm":
                await callback.message.answer("Подтвердите: товары, наличие и розничная сумма из карточки согласованы с клиентом. "
                    "Заявленные сайтом скидки не применяются автоматически.", reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[{"text": "Да, согласовано", "callback_data": f"desk:accept:{draft_id}:{int(parts[3])}"}]]))
                return
            elif action == "accept":
                await service.confirm(draft_id, callback.from_user.id, callback.message.chat.id, int(parts[3]))
            elif action == "cancel":
                await callback.message.answer("Отменить заявку?", reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[{"text": "Да, отменить", "callback_data": f"desk:cancelled:{draft_id}"}]]))
                return
            elif action == "cancelled":
                await service.cancel(draft_id, callback.from_user.id, callback.message.chat.id)
            elif action != "refresh":
                return
            text, keyboard = await desk_card(draft_id)
            from aiogram.exceptions import TelegramBadRequest
            try:
                await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=None)
            except TelegramBadRequest as error:
                if "message is not modified" not in str(error).lower():
                    raise
        except (ValueError, IndexError) as error:
            await callback.message.answer(str(error) if isinstance(error, DeskError) else "Действие недоступно. Обновите карточку.", parse_mode=None)
