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


def product_search_keyboard(draft_id, position, results):
    buttons = []
    for p in results["products"]:
        data = f"desk:bind:{draft_id}:{position}:{results['revision']}:{p['id']}"
        if len(data.encode("utf-8")) > 64:
            continue
        label = (p["name"] or p["article"] or p["id"])[:52]
        if p["article"]:
            label += " · " + p["article"][:20]
        buttons.append([{"text": label[:64], "callback_data": data}])
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


def register(dp, check_access, safe_callback_answer):
    @dp.message(Command("sitematch"))
    async def site_match_command(message):
        if not await check_access(message) or not await desk_access(message.from_user.id, message.chat.id):
            return
        try:
            parts = (message.text or "").split(maxsplit=3)
            if len(parts) != 4:
                raise DeskError("Формат: /sitematch ЗАЯВКА НОМЕР_ПОЗИЦИИ название или артикул")
            _, draft_id, position, query = parts
            draft_id, position = int(draft_id), int(position)
            results = await DeskService().search_site_products(draft_id, message.from_user.id, message.chat.id, query)
            keyboard = product_search_keyboard(draft_id, position, results)
            if not keyboard:
                await message.answer(
                    "Товар не найден по запросу или его ID слишком длинный. "
                    "Попробуйте другое название / артикул: /sitematch " + str(draft_id)
                    + " " + str(position) + " СЛОВО", parse_mode=None)
                return
            await message.answer("Выберите соответствующий товар из МойСклад. "
                "Остаток не влияет на сопоставление. Проверьте полное название и артикул:",
                reply_markup=keyboard, parse_mode=None)
        except (DeskError, ValueError) as error:
            await message.answer(str(error) if isinstance(error, DeskError)
                else "Формат: /sitematch ЗАЯВКА НОМЕР_ПОЗИЦИИ название", parse_mode=None)
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
            elif action == "find":
                from app.models.draft_order import DraftOrder
                from app.database.session import async_session
                from sqlalchemy import select
                from sqlalchemy.orm import selectinload
                async with async_session() as session:
                    draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id)
                        .options(selectinload(DraftOrder.items)))
                    unresolved = ([(pos, item) for pos, item in enumerate(
                        sorted(draft.items, key=lambda i: i.id), 1)
                        if item.match_status != "matched"] if draft else [])
                if not unresolved:
                    raise DeskError("Все позиции уже сопоставлены. Обновите карточку.")
                position, item = unresolved[0]
                query = (item.raw_product_text or "").split("/")[0].strip().split()
                query = " ".join(query[:2])[:120]
                results = await service.search_site_products(draft_id, callback.from_user.id, callback.message.chat.id, query)
                keyboard = product_search_keyboard(draft_id, position, results)
                if keyboard:
                    await callback.message.answer(
                        f"Заявка №{draft_id}, позиция {position}. Выберите правильный товар из МойСклад "
                        f"(это не резервирование):", reply_markup=keyboard, parse_mode=None)
                else:
                    await callback.message.answer(
                        f"Не нашёл совпадений по первым словам названия. Поиск вручную: "
                        f"/sitematch {draft_id} {position} НАЗВАНИЕ_ИЛИ_АРТИКУЛ", parse_mode=None)
                return
            elif action == "bind":
                if len(parts) != 6:
                    raise DeskError("Некорректные данные выбора товара.")
                result = await service.bind_site_product(draft_id, callback.from_user.id,
                    callback.message.chat.id, int(parts[3]), int(parts[4]), parts[5])
                await callback.message.answer(
                    "Товар сопоставлен с МойСклад. " +
                    ("Остались проверки: " + ", ".join(result["remaining_problems"])
                     if result["remaining_problems"]
                     else "Теперь можно подтвердить заказ после проверки цены."),
                    parse_mode=None)
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
            elif action == "recheck":
                result = await service.reprice(draft_id, callback.from_user.id, callback.message.chat.id,
                                               dry_run=False)
                if result["problems"]:
                    from app.services.desk_display import REVIEW_PROBLEM_LABELS
                    messages = [REVIEW_PROBLEM_LABELS.get(code, code) for code in result["problems"]]
                    await callback.message.answer(
                        "Проверка заявки №" + str(draft_id) + ":\n"
                        + "\n".join("• " + text for text in messages[:10])
                        + "\n\nЕсли сумма отсутствует или не совпадает, проверьте передачу суммы в Tilda; "
                          "нельзя подтверждать такой заказ без исправления исходных данных.",
                        parse_mode=None, protect_content=True)
                else:
                    await callback.message.answer(
                        "Проверка завершена. Теперь можно нажать «Подтвердить заказ».",
                        parse_mode=None)
            elif action == "payment":
                await service.stage(draft_id, callback.from_user.id, callback.message.chat.id,
                                    "awaiting_payment")
            elif action == "confirm":
                await callback.message.answer("Подтвердите: товары, количество и розничная сумма согласованы с клиентом. Источник товара будет выбран отдельно после подтверждения. "
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
            text, keyboard = await desk_card(draft_id)
            from aiogram.exceptions import TelegramBadRequest
            try:
                await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=None)
            except TelegramBadRequest as error:
                if "message is not modified" not in str(error).lower():
                    raise
        except (ValueError, IndexError) as error:
            await callback.message.answer(str(error) if isinstance(error, DeskError) else "Действие недоступно. Обновите карточку.", parse_mode=None)
