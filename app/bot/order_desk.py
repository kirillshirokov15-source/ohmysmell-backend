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


async def product_matches(draft_id, item_id, actor, chat_id, query=None):
    """Read-only verified catalogue suggestions, never automatic supplier selection."""
    from app.database.session import async_session
    from app.models.draft_order import DraftOrder, DraftOrderItem
    from app.models.order_desk import OrderDesk
    from app.services.order_desk import require_owner
    from app.services.product_matching_service import ProductMatchingService
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    async with async_session() as session:
        await authorized_manager(session, actor, chat_id)
        desk = await session.get(OrderDesk, draft_id)
        draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id)
            .options(selectinload(DraftOrder.items)))
        if not draft or not desk or desk.order_id or draft.status == "rejected":
            raise DeskError("Заявка закрыта.")
        require_owner(desk, actor)
        item = next((i for i in draft.items if i.id == item_id), None)
        if not item:
            raise DeskError("Позиция отсутствует в заявке.")
        term = (query or item.raw_product_text or "").strip()
    matcher = ProductMatchingService()
    products = await matcher.products_async()
    normalized = matcher.normalize(term)
    tokens = [w for w in normalized.split() if len(w) >= 3 and w not in {"the", "with", "set", "and", "для", "набор"}]
    if not tokens:
        raise DeskError("Введите хотя бы одно слово для поиска товара.")
    ranked = []
    for product in products:
        name = matcher.normalize(product.get("name"))
        article = matcher.normalize(product.get("article"))
        code = matcher.normalize(product.get("code"))
        score = 10 if normalized and normalized in (article, code, name) else 0
        score += sum(4 for word in tokens if word in name.split() or word in (article, code))
        score += sum(1 for word in tokens if word in name and word not in name.split())
        if score > 0 and product.get("id"):
            ranked.append((score,product))
    ranked.sort(key=lambda pair: (-pair[0], (pair[1].get("name") or "").casefold()))
    rows=[]
    for _,product in ranked[:8]:
        rows.append([{"text": (product.get("name") or "Товар")[:45] +
                     ((" · " + str(product.get("article")))[:20] if product.get("article") else ""),
                     "callback_data": f"desk:match:{draft_id}:{item_id}:{product['id']}"}])
    text = f"Заявка №{draft_id}: выберите точный товар из МойСклад для строки:\n{item.raw_product_text[:160]}\n"
    if not rows:
        text += (f"Ничего не найдено по запросу «{term[:90]}».\n"
                 f"Ищите более коротко: /sitematch {draft_id} {item_id} Boadicea")
    else:
        text += ("Выбор — только привязка номенклатуры. Источник (свой/X/поставщик) "
                 "выбирается отдельно после подтверждения заказа.")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


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
            await message.answer(text, reply_markup=keyboard, parse_mode=None)
        except (IndexError, ValueError) as error:
            await message.answer(str(error) if isinstance(error, DeskError) else
                "Формат: /site ЗАЯВКА; /reply ЗАЯВКА текст; /sitereprice ЗАЯВКА; /outbox_retry ID; /outbox_sent ID TELEGRAM_MESSAGE_ID", parse_mode=None)

    @dp.message(Command("sitematch"))
    async def manual_lookup(message):
        if not await check_access(message):
            return
        if not await desk_access(message.from_user.id, message.chat.id):
            return
        try:
            parts = (message.text or "").split(maxsplit=3)
            if len(parts) != 4:
                raise DeskError("Формат: /sitematch НОМЕР_ЗАЯВКИ ID_ПОЗИЦИИ название или артикул")
            text, keyboard = await product_matches(int(parts[1]), int(parts[2]),
                message.from_user.id, message.chat.id, parts[3])
            await message.answer(text, reply_markup=keyboard, parse_mode=None)
        except (DeskError, ValueError) as error:
            await message.answer(str(error) if isinstance(error, DeskError) else "Проверьте номер заявки/позиции.", parse_mode=None)

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
                text, keyboard = await product_matches(draft_id, int(parts[3]),
                    callback.from_user.id, callback.message.chat.id)
                await callback.message.answer(text, reply_markup=keyboard, parse_mode=None, protect_content=True)
                return
            elif action == "match":
                await service.set_manual_product(draft_id, int(parts[3]), parts[4],
                    callback.from_user.id, callback.message.chat.id)
                await callback.message.answer("Товар МойСклад привязан к строке Tilda. "
                    "Источник поставки выберете после подтверждения заказа.", parse_mode=None)
            elif action == "total":
                from app.services.telegram_display import telegram_rubles
                from app.database.session import async_session
                from app.models.draft_order import DraftOrder
                from sqlalchemy import select
                from sqlalchemy.orm import selectinload
                async with async_session() as session:
                    draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id)
                        .options(selectinload(DraftOrder.items)))
                    if not draft or draft.revision != int(parts[3]):
                        raise DeskError("Заявка изменилась. Обновите её.")
                    price = sum(i.price * i.qty for i in draft.items if i.price is not None)
                await callback.message.answer(
                    f"Подтвердите сумму {telegram_rubles(price)} по позициям Tilda для заявки №{draft_id}.\n"
                    "Итог Tilda отсутствует. Вы должны согласовать сумму и товары с покупателем. "
                    "Это НЕ подтверждение оплаты и НЕ создание отгрузки.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[{
                        "text": "Да, сумма согласована", "callback_data": f"desk:totalaccept:{draft_id}:{int(parts[3])}"
                    }]]),parse_mode=None)
                return
            elif action == "totalaccept":
                await service.approve_missing_total(draft_id, int(parts[3]),
                    callback.from_user.id, callback.message.chat.id)
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
            text, keyboard = await desk_card(draft_id)
            from aiogram.exceptions import TelegramBadRequest
            try:
                await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=None)
            except TelegramBadRequest as error:
                if "message is not modified" not in str(error).lower():
                    raise
        except (ValueError, IndexError, KeyError) as error:
            await callback.message.answer(str(error) if isinstance(error, DeskError) else "Действие недоступно. Обновите карточку.", parse_mode=None)
