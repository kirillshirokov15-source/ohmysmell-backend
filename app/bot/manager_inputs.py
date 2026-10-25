"""Explicit replies only, bound to actor, chat and bot prompt; ordinary chat is untouched."""
import time
import logging
from aiogram.types import ForceReply, ReplyParameters, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest

pending = {}
logger = logging.getLogger(__name__)


def waiting(message):
    state = pending.get((message.chat.id, message.from_user.id))
    return bool(state and state[0] > time.monotonic() and message.reply_to_message
                and message.reply_to_message.message_id == state[1])


async def prompt_input(message, action, data, text):
    from html import escape
    sent = await message.answer(f'<a href="tg://user?id={message.from_user.id}">Менеджер</a>, {escape(text)}',
        parse_mode="HTML", reply_markup=ForceReply(selective=True),
        reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=False))
    pending[(message.chat.id, message.from_user.id)] = (time.monotonic() + 600, sent.message_id, action, data)


async def complete_source_prompt(message, data, prompt_id, label, row):
    """Replace original order card; never leave a quoted DELETED bot prompt.

    The user's text reply is removed *before* its temporary ForceReply
    prompt. If the bot lacks delete-message rights, leave the prompt in place
    (edited as completed) so Telegram never displays 'Deleted message' as the
    parent of a surviving manager reply.
    """
    markup = InlineKeyboardMarkup(inline_keyboard=[row])
    origin_id = data.get("origin_message_id")
    updated = False
    if origin_id:
        from app.services.telegram_cards import enabled, display_changed
        if enabled():
            # The ForceReply completion edits the existing order card outside
            # publish(); invalidate its receipt before the Telegram operation.
            await display_changed(message.chat.id, origin_id)
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id, message_id=origin_id, text=label,
                parse_mode=None, reply_markup=markup)
            updated = True
        except TelegramBadRequest as error:
            if "message is not modified" in str(error).casefold():
                updated = True
            else:
                # If the original card disappeared, preserve the confirmed
                # input on a replacement, rather than silently discarding it.
                await message.answer(label, reply_markup=markup, parse_mode=None)
                updated = True
    else:
        await message.answer(label, reply_markup=markup, parse_mode=None)
        updated = True

    if not updated:
        return False

    reply_deleted = False
    try:
        await message.bot.delete_message(chat_id=message.chat.id,
                                         message_id=message.message_id)
        reply_deleted = True
    except TelegramBadRequest:
        # Bot may lack delete rights in a Telegram supergroup.
        pass
    if reply_deleted:
        try:
            await message.bot.delete_message(chat_id=message.chat.id,
                                             message_id=prompt_id)
        except TelegramBadRequest:
            # Bot message may already be removed. No user reply remains.
            pass
    else:
        try:
            # Do NOT delete a prompt while the reply quoting it remains.
            await message.bot.edit_message_text(
                chat_id=message.chat.id, message_id=prompt_id,
                text="✅ Ввод принят. Продолжите в обновлённой карточке заказа.",
                parse_mode=None)
        except TelegramBadRequest:
            pass
    return True


async def route(message, check_access):
    from app.bot.manager_menu import MENU_TEXTS
    if message.text and (message.text.startswith("/") or message.text in MENU_TEXTS):
        pending.pop((message.chat.id, message.from_user.id), None)
        return False
    if not waiting(message):
        return False
    if not await check_access(message):
        return True
    key = (message.chat.id, message.from_user.id)
    _, _, action, data = pending[key]
    if action in {"source_quantity", "source_cost"}:
        logger.info("manager_source_input_received order_id=%s action=%s", data.get("order_id"), action)
    if not message.text:
        await message.answer("Ответьте текстом.")
        return True
    try:
        if action == "reply":
            from app.services.order_desk import DeskService
            await DeskService().reply(data["draft_id"], message.from_user.id, message.chat.id, message.message_id, message.text)
            await message.answer("Сообщение передано клиенту.")
        elif action in {"qtymanual", "searchhint", "cpmanual", "cpnew"}:
            from app.services.draft_order_service import DraftOrderService, DraftOrderError
            from app.services.draft_telegram_service import build_draft_card, build_draft_keyboard
            service = DraftOrderService()
            draft = await service._get_required(data["draft_id"])
            if draft.revision != data["revision"] or draft.status in {"new", "rejected"} or draft.finalized_order_id:
                await message.answer("Заявка изменилась. Откройте актуальную карточку и повторите действие.")
                pending.pop(key, None)
                return True
            if action in {"cpmanual", "cpnew"} and (draft.source != "email" or draft.counterparty_id):
                pending.pop(key, None)
                await message.answer("Контрагент уже привязан или заявка больше не доступна.")
                return True
            if action == "cpnew":
                from app.services.counterparty_creation_service import CounterpartyCreationService, CounterpartyCreationError
                try:
                    prepared = await CounterpartyCreationService().preview(
                        data["draft_id"], message.text, message.from_user.id)
                except CounterpartyCreationError as error:
                    await message.answer(str(error).replace(
                        "Используйте /cpsearch НОМЕР НАЗВАНИЕ", "Воспользуйтесь кнопкой ручного поиска"))
                    return True
                markup = InlineKeyboardMarkup(inline_keyboard=[[
                    {"text": "✅ Создать контрагента в МойСклад",
                     "callback_data": f"draft:cpcreate:{prepared.id}:v{prepared.revision}"},
                    {"text": "Отмена", "callback_data": f"draft:actions:{prepared.id}"}
                ]])
                await message.answer("Проверьте полное название: " + " ".join(message.text.split())[:180]
                                     + "\nНовая карточка пока НЕ создана. "
                                       "Дубли проверены; подтвердите создание отдельной кнопкой.",
                                     parse_mode=None, reply_markup=markup)
            else:
                service.repository.expected_revision = data["revision"]
                if action == "cpmanual":
                    draft = await service.search_counterparties(data["draft_id"], message.text)
                elif action == "searchhint":
                    draft = await service.search_item(data["draft_id"], data["item_id"], message.text)
                else:
                    if not message.text.isascii() or not message.text.isdecimal() or not 0 < int(message.text) <= 10000:
                        await message.answer("Введите количество от 1 до 10000.")
                        return True
                    draft = await service.repository.correct_quantity(
                        data["draft_id"], data["item_id"], int(message.text), message.from_user.id)
                    draft = await service.review(data["draft_id"], draft)
                if action == "cpmanual":
                    from app.services.draft_telegram_service import (
                        counterparty_review_card, counterparty_review_keyboard)
                    await message.answer(counterparty_review_card(draft, manual_search=True),
                        parse_mode=None,
                        reply_markup=InlineKeyboardMarkup.model_validate(
                            counterparty_review_keyboard(draft)))
                else:
                    await message.answer(build_draft_card(draft), parse_mode=None,
                        reply_markup=InlineKeyboardMarkup.model_validate(build_draft_keyboard(draft)))
        elif action in {"buyer_search", "buyer_new_name"}:
            from app.bot.source_navigation import button
            entered = " ".join(message.text.strip().split())
            if (not 3 <= len(entered) <= (150 if action == "buyer_search" else 180)
                    or (action == "buyer_new_name" and len(entered.split()) < 2)):
                await message.answer("Введите имя и фамилию либо поисковый запрос от 3 символов.")
                return True
            from app.services.retail_buyer_binding import RetailBuyerService
            current = await RetailBuyerService()._read(
                data["order_id"], message.from_user.id, message.chat.id)
            if current["revision"] != data["revision"] or current["counterparty_id"]:
                pending.pop(key, None)
                await message.answer("Заказ обновился или покупатель уже сопоставлен. Откройте его снова.")
                return True
            next_state = dict(data, step="buyer_candidates", query=entered) if action == "buyer_search" else (
                dict(data, step="buyer_create_preview", buyer_name=entered))
            row = button("Посмотреть результаты" if action == "buyer_search"
                         else "Проверить дубли перед созданием", next_state)
            await complete_source_prompt(message, data, pending[key][1],
                ("Поиск покупателя для заказа №" if action == "buyer_search"
                 else "Новый покупатель для заказа №") + str(data["order_id"]), row)
        elif action == "own_reserve_number":
            import re
            entered = message.text.strip()
            if not re.fullmatch(r"[0-9]{1,18}", entered):
                await message.answer("Введите только цифровой номер заказа МойСклад, например 00001.")
                return True
            from app.services.order_source_service import OrderSourceService
            view = await OrderSourceService().read(
                data["order_id"], message.from_user.id, message.chat.id)
            if view["revision"] != data["revision"]:
                pending.pop(key, None)
                await message.answer("Заказ изменился. Откройте актуальную карточку №"
                                     + str(data["order_id"]))
                return True
            from app.bot.source_navigation import button
            row = button("Проверить заказ МойСклад №" + entered + " · только чтение",
                dict(data, step="own_reserve_reconcile_by_number",
                     document_number=entered))
            await complete_source_prompt(message, data, pending[key][1],
                "Сверка существующего документа для заказа №" + str(data["order_id"]), row)
        elif action == "source_quantity":
            if not message.text.isascii() or not message.text.isdecimal() or not 0 < int(message.text) <= data["qty"]:
                await message.answer(f"Введите количество от 1 до {data['qty']}.")
                return True
            from app.bot.source_navigation import button
            row = button(f"Далее: {int(message.text)} шт.", dict(data, qty=int(message.text), step=data.get("quantity_next", "apply"), action="assign"))
            await complete_source_prompt(message, data, pending[key][1],
                                         "Проверьте количество для заказа №"
                                         + str(data["order_id"]), row)
        elif action == "source_cost":
            from decimal import Decimal, InvalidOperation
            try:
                price = Decimal(message.text.replace(",", "."))
                if not price.is_finite() or price < 0 or price * 100 != (price * 100).to_integral_value() or price > 1_000_000_000:
                    raise ValueError()
            except (InvalidOperation, ValueError):
                await message.answer("Введите цену в рублях, например 1250,50.")
                return True
            from app.bot.source_navigation import button
            if "allocation_id" not in data:
                # Old pending ForceReply states from pre-cutover cannot silently
                # create a new X settlement or overwrite the Tilda retail price.
                await message.answer(
                    "Старый запрос цены устарел. Откройте заказ и используйте кнопку "
                    "«Указать закупочную себестоимость Х».")
                pending.pop(key, None)
                return True
            row = button("✅ Подтвердить себестоимость Х · " + str(price) + " ₽/шт.",
                dict(data, x_unit_cost_minor=int(price * 100), step="apply", action="set_x_cost"))
            await complete_source_prompt(message, data, pending[key][1],
                                         "Заказ №" + str(data["order_id"])
                                         + " · себестоимость склада Х\n"
                                         + "Закупочная цена: " + str(price) + " ₽/шт.\n"
                                         + "Цена продажи из Tilda остаётся неизменной.\n"
                                         + "Подтвердите только фактическую закупочную цену.",
                                         row)
        pending.pop(key, None)
        if action in {"source_quantity", "source_cost"}:
            logger.info("manager_source_input_confirm_ready order_id=%s action=%s", data.get("order_id"), action)
    except Exception as error:
        from app.services.order_desk import DeskError
        from app.services.draft_order_service import DraftOrderError
        from app.services.order_lifecycle import InvalidOrderTransitionError
        if not isinstance(error, (ValueError, DeskError, DraftOrderError, InvalidOrderTransitionError)):
            raise
        if isinstance(error, (DeskError, DraftOrderError, InvalidOrderTransitionError)):
            await message.answer(str(error)[:350])
        else:
            await message.answer("Проверьте данные и повторите ответ.")
    return True
