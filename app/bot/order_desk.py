"""Explicit group commands only. Ordinary group text never becomes a customer reply."""
from aiogram import F
from aiogram.types import InlineKeyboardMarkup
from aiogram.filters import Command
from app.bot.manager_group import allowed_event
from app.services.order_desk import DeskService, DeskError, authorized_manager, audit, now
from app.services.desk_display import desk_card


async def desk_access(actor, chat_id):
    from app.database.session import async_session
    from time import perf_counter
    from app.services.telegram_metrics import observe
    started = perf_counter()
    try:
        async with async_session() as session:
            await authorized_manager(session, actor, chat_id)
    except DeskError:
        return False
    finally:
        observe("authorization", started)
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
                if len(parts) > 2 and parts[2] != "dry-run":
                    raise DeskError("Формат: /sitereprice ЗАЯВКА [dry-run]")
                result = await service.reprice(identifier, message.from_user.id, message.chat.id,
                                              dry_run=len(parts) > 2)
                if len(parts) > 2:
                    await message.answer(
                        f"Проверка без записи №{identifier}: изменения={result['changed']}; "
                        f"ревизия={result['revision']}; сумма, коп.={result['total_minor']}; "
                        f"проблемы={', '.join(result['problems']) or 'нет'}. "
                        "Подтверждение менеджера обязательно.", parse_mode=None)
                    return
            text, keyboard = await desk_card(identifier)
            from app.services.telegram_cards import publish
            await publish(message.bot, message.chat.id, f"draft:{identifier}", text, keyboard)
        except (IndexError, ValueError) as error:
            await message.answer(str(error) if isinstance(error, DeskError) else
                "Формат: /site ЗАЯВКА; /reply ЗАЯВКА текст; /sitereprice ЗАЯВКА; /outbox_retry ID; /outbox_sent ID TELEGRAM_MESSAGE_ID", parse_mode=None)

    @dp.callback_query(F.data.startswith("desk:"))
    async def desk_callback(callback):
        if not allowed_event(callback, callback=True):
            return
        if not await safe_callback_answer(callback):
            return
        if not await desk_access(callback.from_user.id, callback.message.chat.id):
            return
        from app.services.telegram_cards import obsolete, publish
        if await obsolete(callback.message.chat.id, callback.message.message_id):
            return
        try:
            parts = callback.data.split(":")
            action, draft_id = parts[1], int(parts[2])
            service = DeskService()
            if action == "claim":
                await service.claim(draft_id, callback.from_user.id, callback.message.chat.id)
            elif action in {"sourcedata", "src"}:
                from app.services.tilda_source_review import record
                await record(draft_id, callback.from_user.id, callback.message.chat.id, int(parts[3]),
                    item_id=int(parts[4]) if action == "src" else None,
                    source=parts[5] if action == "src" else None)
            elif action == "checksource":
                from app.repositories.draft_order_repository import DraftOrderRepository
                from app.services.telegram_display import telegram_rubles
                draft = await DraftOrderRepository().get(draft_id)
                lines = ["Подтвердите полученные с сайта позиции:"] + [
                    f"{i.raw_product_text} · {i.qty} шт. × {telegram_rubles(i.price) if i.price is not None else 'цена не указана'}"
                    for i in draft.items]
                lines.append("Это подтверждение исходных данных; наличие и источник проверяются отдельно.")
                await callback.message.answer("\n".join(lines)[:3900], reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    {"text": "Подтвердить", "callback_data": f"desk:sourcedata:{draft_id}:{draft.revision}"},
                    {"text": "Назад", "callback_data": f"desk:refresh:{draft_id}"}]]))
                return
            elif action == "recheck":
                await service.reprice(draft_id, callback.from_user.id, callback.message.chat.id)
            elif action == "findproduct":
                from app.bot.manager_inputs import prompt_input
                message = callback.message.model_copy(update={"from_user": callback.from_user}).as_(callback.bot)
                await prompt_input(message, "tilda_product", {"draft_id": draft_id, "revision": int(parts[3]), "item_id": int(parts[4])},
                    "Введите название товара. Поиск включает товары без остатка.")
                return
            elif action == "bindproduct":
                from app.services.tilda_source_review import bind
                from app.repositories.draft_order_repository import DraftOrderRepository
                draft = await DraftOrderRepository().get(draft_id)
                item = next(i for i in draft.items if i.id == int(parts[4]))
                candidate = item.candidates[int(parts[5])]
                await bind(draft_id, item.id, candidate["id"], callback.from_user.id, callback.message.chat.id, int(parts[3]))
                await service.reprice(draft_id, callback.from_user.id, callback.message.chat.id)
            elif action == "newproduct":
                await callback.message.answer("Подтвердите: это собственный физический товар X; соответствующей карточки в каталоге нет. Создать карточку без остатков?",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        {"text": "Да, создать карточку товара", "callback_data": f"desk:createproduct:{draft_id}:{parts[3]}:{parts[4]}"},
                        {"text": "Назад", "callback_data": f"desk:actions:{draft_id}"}]]))
                return
            elif action == "createproduct":
                from app.services.manager_directory import create_product
                revision = await create_product(draft_id, int(parts[4]), callback.from_user.id,
                    callback.message.chat.id, int(parts[3]))
                await callback.message.answer("Карточка создана без остатка. Подтвердите соответствие позиции:",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        {"text": "Подтвердить соответствие", "callback_data": f"desk:bindproduct:{draft_id}:{revision}:{parts[4]}:0"}]]))
                return
            elif action == "items":
                message = callback.message.model_copy(update={"from_user": callback.from_user, "text": f"/siteitems {draft_id}"}).as_(callback.bot)
                await desk_command(message)
                return
            elif action == "reply":
                from app.bot.manager_inputs import prompt_input
                message = callback.message.model_copy(update={"from_user": callback.from_user}).as_(callback.bot)
                await prompt_input(message, "reply", {"draft_id": draft_id}, f"Ответьте на это сообщение — текст получит клиент по заявке №{draft_id}.")
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
            elif action not in {"refresh", "details", "actions"}:
                return
            text, keyboard = await desk_card(draft_id, view=action if action in {"details", "actions"} else "compact")
            await publish(callback.bot, callback.message.chat.id, f"draft:{draft_id}", text, keyboard)
        except (ValueError, IndexError) as error:
            await callback.message.answer(str(error) if isinstance(error, DeskError) else "Действие недоступно. Обновите карточку.", parse_mode=None)
