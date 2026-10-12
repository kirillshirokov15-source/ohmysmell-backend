"""Durable Telegram sends. Unknown outcomes require explicit reconciliation."""
import asyncio
import logging
import os
from datetime import timedelta
from sqlalchemy import select, update, or_
from sqlalchemy.orm import aliased
from types import SimpleNamespace
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter, TelegramUnauthorizedError
from app.database.session import async_session
from app.models.order_desk import DeskMessage
from app.services.order_desk import now, manager_chat
from app.logging_utils import log_event
from app.services.desk_delivery_policy import DeskDeliveryPolicy

logger = logging.getLogger(__name__)


async def deliver_client_card(bot, message):
    # Receipts are durable and scoped to this order AND destination. No in-memory cache.
    async with async_session() as session:
        previous = await session.scalar(select(DeskMessage).where(
            DeskMessage.draft_id == message.draft_id,
            DeskMessage.destination == message.destination,
            DeskMessage.direction == "to_customer", DeskMessage.kind == "client_card",
            DeskMessage.status == "sent", DeskMessage.telegram_message_id.is_not(None),
            DeskMessage.id < message.id).order_by(DeskMessage.id.desc()).limit(1))
    if previous:
        try:
            await bot.edit_message_text(chat_id=message.destination, message_id=previous.telegram_message_id,
                text=message.body, parse_mode=None, request_timeout=30)
            return SimpleNamespace(message_id=previous.telegram_message_id)
        except TelegramBadRequest as error:
            reason = str(error).lower()
            if "message is not modified" in reason:
                return SimpleNamespace(message_id=previous.telegram_message_id)
            if not any(value in reason for value in ("message to edit not found", "message can't be edited")):
                raise
            # Only a definite edit rejection permits sending a replacement.
    return await bot.send_message(message.destination, message.body, parse_mode=None,
        protect_content=True, disable_web_page_preview=True, request_timeout=30)


async def run_once(bot, role):
    if os.getenv("ORDER_DESK_SEND_ENABLED", "false").lower() != "true":
        return False
    policy = DeskDeliveryPolicy.load(role)
    direction = "to_manager" if role == "manager" else "to_customer"
    async with async_session() as session, session.begin():
        # A crash may happen after Telegram accepted a send. Never blindly resend it.
        await session.execute(update(DeskMessage).where(DeskMessage.direction == direction,
            policy.sql_scope(), DeskMessage.status == "sending", DeskMessage.available_at < now()).values(status="uncertain", error_code="lease_expired"))
        earlier = aliased(DeskMessage)
        ordered = ~select(earlier.id).where(earlier.direction == direction,
            earlier.destination == DeskMessage.destination, earlier.draft_id == DeskMessage.draft_id,
            earlier.kind == "client_card",
            earlier.id < DeskMessage.id, earlier.status.in_(("pending", "sending", "uncertain"))).exists()
        message = await session.scalar(select(DeskMessage).where(DeskMessage.direction == direction,
            policy.sql_scope(),
            or_(DeskMessage.kind != "client_card", ordered) if role == "client" else True,
            DeskMessage.status == "pending", DeskMessage.available_at <= now())
            .order_by(DeskMessage.id).limit(1).with_for_update(skip_locked=True))
        if not message:
            return False
        message.status, message.attempts = "sending", message.attempts + 1
        message.available_at = now() + timedelta(minutes=5)
    try:
        keyboard = None
        if role == "manager" and message.destination != manager_chat():
            raise ValueError("manager_destination_changed")
        body = message.body
        if message.kind == "card":
            from app.services.desk_display import desk_card
            body, keyboard = await desk_card(message.draft_id)
        elif role == "manager" and message.sender_type == "customer" and message.draft_id:
            from aiogram.types import InlineKeyboardMarkup
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[{
                "text": "Ответить клиенту", "callback_data": f"desk:reply:{message.draft_id}"}]])
        if role == "client" and message.kind == "client_menu":
            from app.bot.client_menu import delivery_keyboard
            async with async_session() as session:
                keyboard = await delivery_keyboard(session, message.destination)
        if role == "client" and message.kind == "client_card":
            sent = await deliver_client_card(bot, message)
        else:
            sent = await bot.send_message(message.destination, body, parse_mode=None, reply_markup=keyboard,
                protect_content=True, disable_web_page_preview=True, request_timeout=30)
    except TelegramRetryAfter as error:
        state, reason, delay = "pending", "rate_limit", max(error.retry_after, 1)
    except TelegramForbiddenError:
        state, reason, delay = "blocked", "bot_blocked_or_removed", 0
    except (TelegramBadRequest, TelegramUnauthorizedError, ValueError):
        state, reason, delay = "failed", "invalid_destination_or_request", 0
    except Exception:
        # Network timeout/server failure can be an accepted send with a lost response.
        state, reason, delay = "uncertain", "unknown_send_outcome", 0
    else:
        state, reason, delay = "sent", None, 0
    async with async_session() as session, session.begin():
        current = await session.get(DeskMessage, message.id, with_for_update=True)
        # An operator may have reconciled an expired lease while this send was
        # in flight. An old attempt must never overwrite that decision/receipt.
        if current.attempts != message.attempts or not (current.status == "sending" or
                (current.status == "uncertain" and current.error_code == "lease_expired")):
            log_event(logger, "desk_delivery_late_result", message_id=message.id,
                      draft_id=message.draft_id, result=state)
            return True
        current.status, current.error_code = state, reason
        current.available_at = now() + timedelta(seconds=delay)
        if state == "sent":
            current.telegram_message_id, current.sent_at = sent.message_id, now()
        # Notify managers of failed customer delivery without exposing message content in logs.
        if direction == "to_customer" and message.sender_type == "manager" and state in {"blocked", "failed", "uncertain"}:
            from app.services.order_desk import enqueue
            try:
                chat = manager_chat()
            except ValueError:
                # A missing alarm route must not roll back the known send result.
                log_event(logger, "desk_failure_notification_unavailable", message_id=message.id,
                          draft_id=message.draft_id, result="manager_configuration_invalid")
            else:
                enqueue(session, draft_id=message.draft_id, key=f"delivery-failed:{message.id}:{message.attempts}",
                    direction="to_manager", destination=chat,
                    body=f"Сообщение #{message.id} по заявке №{message.draft_id}: {state}. /outbox_retry ID после проверки доставки.")
    event = ("manager_reply_sent" if state == "sent" else "manager_reply_failed") if message.sender_type == "manager" else (
        "desk_message_sent" if state == "sent" else "desk_delivery_failed")
    log_event(logger, event,
        message_id=message.id, draft_id=message.draft_id, result=state)
    return True


async def main(bot, role):
    while True:
        try:
            processed = await run_once(bot, role)
        except Exception as error:
            log_event(logger, "desk_outbox_failure", result=type(error).__name__)
            processed = False
        await asyncio.sleep(0.2 if processed else 2)
