"""Explicit replies only, bound to actor, chat and bot prompt; ordinary chat is untouched."""
import time
from aiogram.types import ForceReply, ReplyParameters, InlineKeyboardMarkup

pending = {}
processing = set()
controls = {}


async def close_prompt(message, prompt_id):
    from aiogram.exceptions import TelegramBadRequest
    try:
        await message.bot.delete_message(chat_id=message.chat.id, message_id=prompt_id)
    except TelegramBadRequest:
        try:
            await message.bot.edit_message_text(chat_id=message.chat.id, message_id=prompt_id,
                text="Ввод завершён. Откройте действия заявки для нового ввода.", reply_markup=None)
        except Exception:
            pass
    except Exception:
        pass
    control = controls.pop((message.chat.id, prompt_id), None)
    if control:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=control)
        except Exception:
            pass


def waiting(message):
    state = pending.get((message.chat.id, message.from_user.id))
    return bool(state and state[0] > time.monotonic() and message.reply_to_message
                and message.reply_to_message.message_id == state[1])


async def prompt_input(message, action, data, text):
    from html import escape
    old = pending.pop((message.chat.id, message.from_user.id), None)
    if old:
        await close_prompt(message, old[1])
    sent = await message.answer(f'<a href="tg://user?id={message.from_user.id}">Менеджер</a>, {escape(text)}',
        parse_mode="HTML", reply_markup=ForceReply(selective=True),
        reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=False))
    pending[(message.chat.id, message.from_user.id)] = (time.monotonic() + 600, sent.message_id, action, data)
    control = await message.answer("Можно отменить ввод.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        {"text": "Отмена", "callback_data": f"inputcancel:{message.from_user.id}:{sent.message_id}"},
        {"text": "Назад", "callback_data": f"inputback:{message.from_user.id}:{sent.message_id}"}]]))
    controls[(message.chat.id, sent.message_id)] = control.message_id


async def route(message, check_access):
    from app.bot.manager_menu import MENU_TEXTS
    if message.text and (message.text.startswith("/") or message.text in MENU_TEXTS):
        state = pending.pop((message.chat.id, message.from_user.id), None)
        if state:
            await close_prompt(message, state[1])
        return False
    if not waiting(message):
        return False
    if not await check_access(message):
        return True
    key = (message.chat.id, message.from_user.id)
    state = pending.get(key)
    if not state or not waiting(message) or key in processing:
        return True
    _, prompt_id, action, data = state
    if not message.text:
        await message.answer("Ответьте текстом.")
        return True
    processing.add(key)
    try:
        if action == "tilda_product":
            from app.bot.order_desk import desk_access
            if not await desk_access(message.from_user.id, message.chat.id):
                return True
            from app.services.tilda_source_review import search
            draft = await search(data["draft_id"], data["item_id"], message.text,
                message.from_user.id, message.chat.id, data["revision"])
            item = next(i for i in draft.items if i.id == data["item_id"])
            rows = [[{"text": c.get("name", "Товар")[:60],
                "callback_data": f"desk:bindproduct:{draft.id}:{draft.revision}:{item.id}:{n}"}]
                for n, c in enumerate(item.candidates[:5])]
            if not rows:
                await message.answer("Совпадений нет. Повторите ввод названия. До идентификации товара складские операции недоступны.")
                await message.answer("Если это собственный физический товар X, можно проверить создание карточки:",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        {"text": "Создать товар в МойСклад", "callback_data": f"desk:newproduct:{draft.id}:{draft.revision}:{item.id}"}]]))
                pending[key] = (state[0], state[1], action, {**data, "revision": draft.revision})
                return True
            await message.answer("Выберите товар и подтвердите соответствие исходной позиции:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        elif action in {"counterparty", "product", "draft_quantity"}:
            from app.services.draft_order_service import DraftOrderService
            from app.services.draft_telegram_service import build_draft_card, build_draft_keyboard
            service = DraftOrderService()
            service.repository.expected_revision = data["revision"]
            if action == "counterparty":
                candidates = await service.counterparty_service.candidates_async(message.text[:500])
                draft = await service.repository.set_counterparty_candidates(data["draft_id"], candidates)
            elif action == "product":
                draft = await service.search_item(data["draft_id"], data["item_id"], message.text)
            else:
                if not message.text.isascii() or not message.text.isdecimal() or not 0 < int(message.text) <= 10000:
                    await message.answer("Введите целое количество от 1 до 10000.")
                    return True
                draft = await service.repository.correct_quantity(data["draft_id"], data["item_id"],
                    int(message.text), message.from_user.id)
                draft = await service.review(data["draft_id"], draft)
            from app.services.telegram_cards import publish
            await publish(message.bot, message.chat.id, f"draft:{draft.id}", build_draft_card(draft),
                InlineKeyboardMarkup.model_validate(build_draft_keyboard(draft)))
        elif action == "reply":
            from app.services.order_desk import DeskService
            await DeskService().reply(data["draft_id"], message.from_user.id, message.chat.id, message.message_id, message.text)
            await message.answer("Сообщение передано клиенту.")
        elif action == "source_quantity":
            if not message.text.isascii() or not message.text.isdecimal() or not 0 < int(message.text) <= data["qty"]:
                await message.answer(f"Введите количество от 1 до {data['qty']}.")
                return True
            from app.bot.source_navigation import button
            row = button(f"Далее: {int(message.text)} шт.", dict(data, qty=int(message.text), step=data.get("quantity_next", "apply"), action="assign"))
            await message.answer("Проверьте количество", reply_markup=InlineKeyboardMarkup(inline_keyboard=[row]))
        elif action in {"source_cost", "document_cost"}:
            from decimal import Decimal, InvalidOperation
            try:
                price = Decimal(message.text.replace(",", "."))
                if not price.is_finite() or price < 0 or price * 100 != (price * 100).to_integral_value() or price > 1_000_000_000:
                    raise ValueError()
            except (InvalidOperation, ValueError):
                await message.answer("Введите цену в рублях, например 1250,50.")
                return True
            from app.bot.source_navigation import button
            row = (button(f"Подтвердить себестоимость: {price} ₽", dict(data, receipt_cost_minor=int(price * 100), step="document_confirm"))
                if action == "document_cost" else button(f"Подтвердить: {data['qty']} шт. по {price} ₽",
                    dict(data, base_cost_minor=int(price * 100), step="apply", action="assign")))
            await message.answer("Проверьте цену и количество", reply_markup=InlineKeyboardMarkup(inline_keyboard=[row]))
        if pending.get(key) == state:
            pending.pop(key, None)
        await close_prompt(message, prompt_id)
    except ValueError as error:
        from app.services.order_desk import DeskError
        await message.answer(str(error) if isinstance(error, DeskError) else "Проверьте данные и повторите ответ.")
    finally:
        processing.discard(key)
    return True
