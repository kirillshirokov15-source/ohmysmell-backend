"""Send once, persist receipt, then retire only recorded bot cards.

Sending/uncertain attempts block new sends even after a restart. Telegram has
no idempotency key for sendMessage: unknown outcomes need reconciliation.
"""
import hashlib
import os
import json
from time import perf_counter
from types import SimpleNamespace
from sqlalchemy import select
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from app.database.session import async_session
from app.models.telegram_card import TelegramCard, TelegramCardAttempt
from app.services.telegram_metrics import observe


def enabled():
    """Opt-in only after migration and a supervised staging transport test.

    The new service changes Telegram delivery/deletion behavior. Keep it off
    until a release operator explicitly enables it per worker environment.
    """
    return os.getenv("OMS_TELEGRAM_DURABLE_CARDS_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


class CardDeliveryUncertain(RuntimeError):
    pass


def fingerprint(text, keyboard):
    markup = keyboard.model_dump(mode="json", exclude_none=True) if keyboard else None
    return hashlib.sha256(json.dumps([text, markup], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def reconcile(bot, entity, message):
    """Accept only a Telegram-provided reply to this bot's exact pending card.

    Never accept an arbitrary numeric message ID: that could register a human
    message for subsequent deletion. Access checks belong to the caller.
    """
    if not message.from_user or message.from_user.id != bot.id or not message.from_user.is_bot:
        raise ValueError("Ответьте на карточку этого бота.")
    async with async_session() as session, session.begin():
        card = await session.get(TelegramCard, (entity, message.chat.id), with_for_update=True)
        attempt = await session.get(TelegramCardAttempt, card.attempt_id) if card and card.attempt_id else None
        if not attempt or attempt.status not in {"sending", "uncertain"} or attempt.digest != fingerprint(message.text, message.reply_markup):
            raise ValueError("Сообщение не соответствует ожидаемой отправке.")
        if await session.scalar(select(TelegramCardAttempt.id).where(
                TelegramCardAttempt.chat_id == message.chat.id,
                TelegramCardAttempt.message_id == message.message_id).limit(1)):
            raise ValueError("Это сообщение уже относится к другой попытке доставки.")
        previous = (await session.scalars(select(TelegramCardAttempt).where(
            TelegramCardAttempt.entity == entity, TelegramCardAttempt.chat_id == message.chat.id,
            TelegramCardAttempt.status == "sent"))).all()
        for row in previous:
            row.status = "superseded"
        attempt.status, attempt.message_id = "sent", message.message_id
        card.message_id, card.digest = message.message_id, attempt.digest
    await retire(bot, entity, message.chat.id)


async def display_changed(chat_id, message_id):
    from sqlalchemy import update
    async with async_session() as session, session.begin():
        await session.execute(update(TelegramCard).where(TelegramCard.chat_id == chat_id,
            TelegramCard.message_id == message_id).values(digest=None))


async def obsolete(chat_id, message_id):
    async with async_session() as session:
        return bool(await session.scalar(select(TelegramCardAttempt.id).where(
            TelegramCardAttempt.chat_id == chat_id, TelegramCardAttempt.message_id == message_id,
            TelegramCardAttempt.status.in_(("superseded", "retired"))).limit(1)))


async def retire(bot, entity, chat_id):
    async with async_session() as session:
        previous = (await session.scalars(select(TelegramCardAttempt).where(
            TelegramCardAttempt.entity == entity, TelegramCardAttempt.chat_id == chat_id,
            TelegramCardAttempt.status == "superseded"))).all()
    for receipt in previous:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=receipt.message_id)
        except TelegramBadRequest:
            try:
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=receipt.message_id, reply_markup=None)
                await bot.edit_message_text(chat_id=chat_id, message_id=receipt.message_id,
                    text="Карточка устарела. Актуальная карточка заявки ниже в чате.", reply_markup=None)
            except Exception:
                continue  # Retry retirement on the next refresh; stale callbacks are blocked.
        except Exception:
            continue
        async with async_session() as session, session.begin():
            row = await session.get(TelegramCardAttempt, receipt.id)
            row.status = "retired"


async def publish(bot, chat_id, entity, text, keyboard=None):
    started = perf_counter()
    digest = fingerprint(text, keyboard)
    async with async_session() as session, session.begin():
        dialect = session.bind.dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        await session.execute(insert(TelegramCard).values(entity=entity, chat_id=chat_id).on_conflict_do_nothing())
        card = await session.get(TelegramCard, (entity, chat_id), with_for_update=True)
        if not card.attempt_id and entity.startswith("draft:"):
            # Adopt durable receipts from the previous outbox implementation.
            # No arbitrary callback/user message IDs enter the retirement set.
            from app.models.order_desk import DeskMessage
            legacy = (await session.scalars(select(DeskMessage).where(
                DeskMessage.draft_id == int(entity.split(":")[1]), DeskMessage.destination == chat_id,
                DeskMessage.direction == "to_manager", DeskMessage.kind == "card", DeskMessage.status == "sent",
                DeskMessage.telegram_message_id.is_not(None)))).all()
            for receipt in {row.telegram_message_id: row for row in legacy}.values():
                session.add(TelegramCardAttempt(entity=entity, chat_id=chat_id, digest="legacy",
                    status="sent", message_id=receipt.telegram_message_id))
        if card.attempt_id:
            attempt = await session.get(TelegramCardAttempt, card.attempt_id)
            if attempt.status in {"sending", "uncertain"}:
                raise CardDeliveryUncertain("Требуется сверка предыдущей отправки карточки.")
        if card.digest == digest and card.message_id:
            known_id = card.message_id
            attempt_id = None
        else:
            attempt = TelegramCardAttempt(entity=entity, chat_id=chat_id, digest=digest, status="sending")
            session.add(attempt)
            await session.flush()
            attempt_id = card.attempt_id = attempt.id
    if attempt_id is None:
        await retire(bot, entity, chat_id)
        return SimpleNamespace(message_id=known_id)
    try:
        sent = await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode=None,
            protect_content=True, disable_web_page_preview=True)
    except BaseException as error:
        # Cancellation/crash after dispatch is also ambiguous. A hard crash leaves
        # 'sending', which has the same fail-closed behavior on restart.
        definitive = isinstance(error, (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter))
        async with async_session() as session, session.begin():
            attempt = await session.get(TelegramCardAttempt, attempt_id)
            attempt.status = "failed" if definitive else "uncertain"
        raise
    async with async_session() as session, session.begin():
        card = await session.get(TelegramCard, (entity, chat_id), with_for_update=True)
        attempt = await session.get(TelegramCardAttempt, attempt_id)
        previous = (await session.scalars(select(TelegramCardAttempt).where(
            TelegramCardAttempt.entity == entity, TelegramCardAttempt.chat_id == chat_id,
            TelegramCardAttempt.status == "sent"))).all()
        for row in previous:
            row.status = "superseded"
        attempt.status, attempt.message_id = "sent", sent.message_id
        card.message_id, card.digest = sent.message_id, digest
    await retire(bot, entity, chat_id)
    observe("card_update", started)
    return sent
