"""Local order helpdesk. No email, stock, payment or MoySklad writes."""
import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database.session import async_session
from app.models.customer import Customer, CustomerIdentity
from app.models.draft_order import DraftOrder, DraftOrderItem
from app.models.inbound_message import InboundMessage
from app.models.manager import Manager
from app.models.order import Order
from app.models.order_desk import OrderDesk, OrderLink, DeskEvent, DeskMessage
from app.models.operations import ClientUpdate
from app.services.checkout_service import transaction_lock
from app.integrations.tilda import digest, product_map
from app.logging_utils import log_event

logger = logging.getLogger(__name__)


class DeskError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc)


def manager_chat():
    from app.bot.manager_group import group_id, allowed_users
    chat = group_id()
    if chat is None or chat >= 0 or not allowed_users():
        raise DeskError("Группа менеджеров не настроена.")
    return chat


async def authorized_manager(session, actor, chat_id):
    from app.bot.manager_group import allowed_users
    if chat_id != manager_chat() or actor not in allowed_users():
        raise DeskError("Нет доступа менеджера.")
    manager = await session.scalar(select(Manager).where(Manager.telegram_id == actor,
        Manager.is_active.is_(True)).with_for_update(read=True))
    if not manager:
        raise DeskError("Нет доступа менеджера.")
    return manager


def require_owner(desk, actor):
    if desk.actor_telegram_id != actor:
        raise DeskError("Действие доступно только ответственному менеджеру. Сначала возьмите заказ.")


def enqueue(session, *, draft_id, key, direction, destination, body="", kind="text",
            sender_type="system", actor=None, source_message_id=None):
    message = DeskMessage(draft_id=draft_id, idempotency_key=key, direction=direction,
        destination=destination, body=body, kind=kind, sender_type=sender_type,
        sender_telegram_id=actor, source_message_id=source_message_id)
    session.add(message)
    return message


def audit(session, desk, action, actor=None, **details):
    session.add(DeskEvent(draft_id=desk.draft_id, action=action, actor_telegram_id=actor, details=details))
    log_event(logger, action, draft_id=desk.draft_id, actor_telegram_id=actor, result="pending_commit")


class TildaIntake:
    async def submit(self, payload):
        fingerprint = digest(payload)
        chat = manager_chat()
        async with async_session() as session, session.begin():
            await transaction_lock(session, "tilda:" + payload["external_id"])
            existing = await session.scalar(select(OrderDesk).where(OrderDesk.external_id == payload["external_id"]))
            if existing:
                if existing.payload_hash != fingerprint:
                    raise DeskError("external_id_conflict")
                log_event(logger, "tilda_duplicate_detected", draft_id=existing.draft_id)
                return {"draft_id": existing.draft_id, "duplicate": True, "status": "accepted"}
            email = payload["email"].casefold()
            customer = None
            if email:
                await transaction_lock(session, "customer-email:" + email)
                customer = await session.scalar(select(Customer).join(CustomerIdentity).where(
                    CustomerIdentity.identity_type == "email", CustomerIdentity.normalized_value == email))
            if customer is None:
                customer = Customer(display_name=payload["name"] or "Клиент сайта", customer_type="retail")
                # Unverified contacts are not login identities; linking is order-scoped.
                session.add(customer)
                await session.flush()
            inbound = InboundMessage(source="website", external_message_id="tilda:" + payload["external_id"],
                sender=email, subject="Заказ Tilda", body_text=json.dumps(payload, ensure_ascii=False),
                received_at=now(), processing_status="processed", customer_id=customer.id)
            session.add(inbound)
            await session.flush()
            mapping = product_map()
            problems = list(payload["problems"])
            draft = DraftOrder(inbound_message_id=inbound.id, customer_id=customer.id, source="website",
                customer_type="retail", customer_name=payload["name"] or "Клиент сайта", sender_email=email,
                subject="Заказ Tilda", status="needs_review", counterparty_candidates=[],
                contact_details={"phone": payload["phone"], "comment": payload["comment"],
                    "tilda": True, "reported_total_minor": payload["reported_total"],
                    "discount_minor": payload["discount"], "currency": payload["currency"],
                    "external_products": [i["externalid"] for i in payload["items"]]})
            for row in payload["items"]:
                product = mapping.get(row["externalid"])
                price = product["retail_price_minor"] if product else None
                if not product or price is None:
                    problems.append("retail_mapping_required")
                elif price != row["price_minor"]:
                    problems.append("retail_price_mismatch")
                draft.items.append(DraftOrderItem(raw_product_text=row["name"], qty=row["qty"],
                    product_id=product["product_id"] if product else None, product_name=row["name"],
                    price=price, item_total=price * row["qty"] if price is not None else None,
                    match_status="matched" if product else "not_found", candidates=[]))
            if payload["discount"]:
                problems.append("discount_requires_agreement")
            draft.total = sum(i.item_total for i in draft.items) if not problems else None
            draft.review_notes = ", ".join(sorted(set(problems))) or "Подтвердите наличие, розничную сумму и условия с клиентом."
            session.add(draft)
            await session.flush()
            desk = OrderDesk(draft_id=draft.id, external_id=payload["external_id"], payload_hash=fingerprint)
            session.add(desk)
            await session.flush()
            enqueue(session, draft_id=draft.id, key=f"tilda:new:{draft.id}", direction="to_manager", destination=chat, kind="card")
            audit(session, desk, "tilda_order_created")
            return {"draft_id": draft.id, "duplicate": False, "status": "needs_review"}


class DeskService:
    async def claim(self, draft_id, actor, chat_id):
        async with async_session() as session, session.begin():
            manager = await authorized_manager(session, actor, chat_id)
            desk = await session.get(OrderDesk, draft_id, with_for_update=True)
            if not desk:
                raise DeskError("Заявка не найдена.")
            if desk.manager_id:
                log_event(logger, "manager_claim_conflicted" if desk.manager_id != manager.id else "manager_claim_replayed", draft_id=draft_id, actor_telegram_id=actor)
                return desk
            desk.manager_id, desk.actor_telegram_id, desk.assigned_at = manager.id, actor, now()
            desk.stage = "working"
            audit(session, desk, "manager_claim_succeeded", actor)
            enqueue(session, draft_id=draft_id, key=f"claim:{draft_id}", direction="to_manager", destination=chat_id,
                body=f"Заявку с сайта №{draft_id} взял менеджер {manager.name or manager.id}.")
            return desk

    async def issue_link(self, draft_id):
        username = os.getenv("CLIENT_TELEGRAM_BOT_USERNAME", "")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            raise DeskError("Клиентский бот не настроен.")
        ttl = int(os.getenv("CLIENT_TELEGRAM_LINK_TTL_MINUTES", "30"))
        if not 1 <= ttl <= 1440:
            raise DeskError("Некорректный срок ссылки.")
        token = secrets.token_urlsafe(32)
        async with async_session() as session, session.begin():
            desk = await session.get(OrderDesk, draft_id, with_for_update=True)
            if not desk or desk.customer_telegram_id:
                raise DeskError("Ссылка недоступна.")
            # Reissuing revokes all unused links, while retaining hashed audit records.
            from sqlalchemy import update
            await session.execute(update(OrderLink).where(OrderLink.draft_id == draft_id,
                OrderLink.consumed_by.is_(None)).values(expires_at=now()))
            session.add(OrderLink(token_hash=hashlib.sha256(token.encode()).hexdigest(), draft_id=draft_id,
                expires_at=now() + timedelta(minutes=ttl)))
        return {"deep_link": f"https://t.me/{username}?start={token}", "expires_in_seconds": ttl * 60}

    async def consume(self, session, token, actor):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise DeskError("Ссылка недействительна или истекла. Свяжитесь с менеджером.")
        link = await session.get(OrderLink, hashlib.sha256(token.encode()).hexdigest())
        if not link:
            raise DeskError("Ссылка недействительна или истекла. Свяжитесь с менеджером.")
        desk = await session.get(OrderDesk, link.draft_id, with_for_update=True)
        await session.refresh(link, with_for_update=True)
        if link.consumed_by == actor and desk.customer_telegram_id == actor:
            return desk
        expires = link.expires_at.replace(tzinfo=timezone.utc) if link.expires_at.tzinfo is None else link.expires_at
        if link.consumed_by or expires <= now() or desk.customer_telegram_id not in (None, actor):
            raise DeskError("Ссылка недействительна или истекла. Свяжитесь с менеджером.")
        desk.customer_telegram_id, desk.linked_at = actor, now()
        link.consumed_by, link.consumed_at = actor, now()
        audit(session, desk, "telegram_customer_linked", actor)
        enqueue(session, draft_id=desk.draft_id, key=f"linked:{desk.draft_id}", direction="to_manager",
            destination=manager_chat(), body=f"К заявке №{desk.draft_id} подключён клиент Telegram. /site {desk.draft_id}")
        return desk

    async def client(self, actor, message_id, text):
        if actor <= 0 or message_id <= 0:
            raise DeskError("Invalid Telegram envelope")
        key = f"desk:client:{actor}:{message_id}"
        async with async_session() as session, session.begin():
            await transaction_lock(session, f"desk:client:{actor}")
            if await session.get(ClientUpdate, key):
                return
            draft_id = None
            try:
                async with session.begin_nested():
                    response, draft_id = await self._client(session, actor, message_id, text, key)
            except DeskError as error:
                response = str(error)
            session.add(ClientUpdate(key=key, response=response))
            enqueue(session, draft_id=draft_id, key=key + ":ack", direction="to_customer", destination=actor, body=response)

    async def _client(self, session, actor, message_id, text, key):
        if text is None:
            return "Поддерживаются только текстовые сообщения. Вложения передайте менеджеру другим согласованным способом.", None
        if text.startswith("/start "):
            desk = await self.consume(session, text.split(maxsplit=1)[1], actor)
            return (f"Спасибо за заказ (заявка №{desk.draft_id})! Мы получили вашу заявку. "
                "В ближайшее время менеджер свяжется с вами, подтвердит наличие товаров и согласует оплату и доставку.\n"
                f"/status {desk.draft_id} — статус; /message {desk.draft_id} текст — написать менеджеру; /orders — мои заказы.", desk.draft_id)
        if text in {"/start", "/help", "/orders", "Мой заказ"}:
            desks = (await session.scalars(select(OrderDesk).where(OrderDesk.customer_telegram_id == actor)
                .order_by(OrderDesk.draft_id.desc()).limit(30))).all()
            return ("Ваши заявки:\n" + "\n".join(f"/status {d.draft_id} · /message {d.draft_id} текст" for d in desks)
                if desks else "Откройте персональную ссылку после оформления заказа. Если ссылки нет, свяжитесь с менеджером.", None)
        parts = text.split(maxsplit=2)
        if parts[0] not in {"/status", "/message", "/manager"} or len(parts) < 2 or not parts[1].isascii() or not parts[1].isdigit() or len(parts[1]) > 9:
            return "Укажите заявку явно: /orders, /status НОМЕР или /message НОМЕР текст. Обычный текст не пересылается.", None
        desk = await session.get(OrderDesk, int(parts[1]))
        if not desk or desk.customer_telegram_id != actor:
            raise DeskError("Заявка не найдена среди ваших заказов.")
        if parts[0] == "/status":
            return await public_status(session, desk), desk.draft_id
        if len(parts) != 3 or not parts[2].strip() or len(parts[2].encode("utf-16-le")) // 2 > 3000:
            raise DeskError("Формат: /message НОМЕР текст (до 3000 символов Telegram).")
        enqueue(session, draft_id=desk.draft_id, key=key, direction="to_manager", destination=manager_chat(),
            body=f"Сообщение от клиента по заявке №{desk.draft_id}:\n{parts[2]}\n\n/reply {desk.draft_id} текст",
            sender_type="customer", actor=actor, source_message_id=message_id)
        audit(session, desk, "customer_message_received", actor)
        return "Сообщение передано менеджеру.", desk.draft_id

    async def reply(self, draft_id, actor, chat_id, message_id, text):
        if not text.strip() or len(text.encode("utf-16-le")) // 2 > 3000:
            raise DeskError("Текст должен содержать от 1 до 3000 символов Telegram.")
        async with async_session() as session, session.begin():
            await authorized_manager(session, actor, chat_id)
            await transaction_lock(session, f"desk:reply:{chat_id}:{message_id}")
            desk = await session.get(OrderDesk, draft_id, with_for_update=True)
            if not desk:
                raise DeskError("Заявка не найдена.")
            require_owner(desk, actor)
            if not desk.customer_telegram_id:
                raise DeskError("Клиент ещё не подключил Telegram. Используйте контактные данные заявки.")
            key = f"desk:reply:{chat_id}:{message_id}"
            previous = await session.scalar(select(DeskMessage).where(DeskMessage.idempotency_key == key))
            if previous:
                if previous.draft_id != draft_id or previous.sender_telegram_id != actor:
                    raise DeskError("Конфликт повторного сообщения.")
                return previous
            message = enqueue(session, draft_id=draft_id, key=key, direction="to_customer", destination=desk.customer_telegram_id,
                body=f"Менеджер по заявке №{draft_id}:\n{text}", sender_type="manager", actor=actor, source_message_id=message_id)
            audit(session, desk, "manager_reply_queued", actor)
            return message

    async def stage(self, draft_id, actor, chat_id, stage):
        if stage not in {"awaiting_confirmation", "awaiting_payment"}:
            raise DeskError("Недопустимый этап.")
        async with async_session() as session, session.begin():
            await authorized_manager(session, actor, chat_id)
            desk = await session.get(OrderDesk, draft_id, with_for_update=True)
            if not desk:
                raise DeskError("Заявка не найдена.")
            require_owner(desk, actor)
            if desk.order_id:
                order = await session.get(Order, desk.order_id)
                if order.fulfillment_status == "cancelled" or order.delivery_status == "delivered" or order.payment_status == "paid":
                    raise DeskError("Этап недоступен для закрытого или оплаченного заказа.")
            else:
                draft = await session.get(DraftOrder, draft_id)
                if draft.status == "rejected":
                    raise DeskError("Заявка отменена.")
            if stage == "awaiting_payment" and not desk.order_id:
                raise DeskError("Сначала подтвердите заявку и сумму.")
            if desk.stage != stage:
                desk.stage = stage
                audit(session, desk, "order_stage_changed", actor, stage=stage)
                if desk.customer_telegram_id:
                    event_key = secrets.token_hex(12)
                    enqueue(session, draft_id=draft_id, key=f"stage:{draft_id}:{event_key}", direction="to_customer",
                        destination=desk.customer_telegram_id, body="Заявка №" + str(draft_id) + ": " +
                        ("Ожидает оплаты. Способ и реквизиты согласуйте с менеджером." if stage == "awaiting_payment" else "Ожидает подтверждения."))

    async def cancel(self, draft_id, actor, chat_id):
        async with async_session() as session, session.begin():
            await authorized_manager(session, actor, chat_id)
            draft = await session.get(DraftOrder, draft_id, with_for_update=True)
            desk = await session.get(OrderDesk, draft_id, with_for_update=True)
            if not desk or not draft or desk.order_id:
                raise DeskError("Отмените подтверждённый заказ в карточке заказа.")
            require_owner(desk, actor)
            if draft.status != "rejected":
                draft.status, draft.revision = "rejected", draft.revision + 1
                audit(session, desk, "tilda_order_cancelled", actor)
                if desk.customer_telegram_id:
                    enqueue(session, draft_id=draft_id, key=f"cancel:{draft_id}", direction="to_customer",
                        destination=desk.customer_telegram_id, body=f"Заявка №{draft_id} отменена менеджером.")

    async def confirm(self, draft_id, actor, chat_id, revision):
        # Lock order: draft -> desk, same as the existing finalization pipeline.
        async with async_session() as session, session.begin():
            await authorized_manager(session, actor, chat_id)
            draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id)
                .options(selectinload(DraftOrder.items)).with_for_update())
            desk = await session.get(OrderDesk, draft_id, with_for_update=True)
            if not draft or not desk:
                raise DeskError("Заявка не найдена.")
            require_owner(desk, actor)
            if not draft.finalized_order_id:
                if draft.revision != revision or draft.status == "rejected":
                    raise DeskError("Карточка устарела или заявка отменена.")
                if any(i.price is None or i.price <= 0 or not i.product_id or i.qty <= 0 for i in draft.items):
                    raise DeskError("Нужны проверенные товары и розничные цены. Исправьте mapping и выполните /sitereprice.")
                draft.total = sum(i.price * i.qty for i in draft.items)
                for item in draft.items:
                    item.item_total = item.price * item.qty
                draft.status = "ready"
                draft.contact_details = {**draft.contact_details, "tilda_review_approved": True}
                audit(session, desk, "tilda_review_approved", actor, total_minor=draft.total, revision=revision)
        from app.repositories.draft_order_repository import DraftOrderRepository
        return await DraftOrderRepository().finalize(draft_id)

    async def reprice(self, draft_id, actor, chat_id):
        mapping = product_map()
        async with async_session() as session, session.begin():
            await authorized_manager(session, actor, chat_id)
            draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id)
                .options(selectinload(DraftOrder.items)).with_for_update())
            desk = await session.get(OrderDesk, draft_id, with_for_update=True)
            if not desk or not draft or draft.finalized_order_id or draft.status == "rejected":
                raise DeskError("Заявка недоступна.")
            require_owner(desk, actor)
            external = draft.contact_details["external_products"]
            for item, identifier in zip(sorted(draft.items, key=lambda i: i.id), external):
                product = mapping.get(identifier)
                item.product_id = product["product_id"] if product else None
                item.price = product["retail_price_minor"] if product else None
                item.item_total = item.price * item.qty if item.price is not None else None
                item.match_status = "matched" if product else "not_found"
            draft.total = None
            draft.status = "needs_review"
            draft.revision += 1
            draft.contact_details = {**draft.contact_details, "tilda_review_approved": False}
            audit(session, desk, "tilda_repriced", actor, revision=draft.revision)


async def public_status(session, desk):
    from app.services.manager_workspace import FULFILLMENT_LABELS, PAYMENT_LABELS
    if desk.order_id:
        order = await session.get(Order, desk.order_id)
        state = "Доставлен" if order.delivery_status == "delivered" else FULFILLMENT_LABELS[order.fulfillment_status]
        if desk.stage == "awaiting_payment" and order.payment_status == "unpaid" and order.fulfillment_status == "new":
            state = "Ожидает оплаты"
        elif order.fulfillment_status == "new" and desk.stage in {"working", "awaiting_confirmation"}:
            state = "В работе" if desk.stage == "working" else "Ожидает подтверждения"
        return f"Заявка №{desk.draft_id}, заказ №{order.id}: {state}. {PAYMENT_LABELS[order.payment_status]}."
    draft = await session.get(DraftOrder, desk.draft_id)
    labels = {"new": "Получен", "working": "В работе", "awaiting_confirmation": "Ожидает подтверждения", "awaiting_payment": "Ожидает оплаты"}
    return f"Заявка №{desk.draft_id}: " + ("Отменена" if draft.status == "rejected" else labels[desk.stage])


async def status_notification(session, order, actor):
    desk = await session.scalar(select(OrderDesk).where(OrderDesk.order_id == order.id))
    if desk:
        audit(session, desk, "order_status_changed", actor, order_id=order.id, revision=order.revision,
            payment=order.payment_status, fulfillment=order.fulfillment_status, delivery=order.delivery_status)
    if desk and desk.customer_telegram_id:
        enqueue(session, draft_id=desk.draft_id, key=f"status:{order.id}:{order.revision}", direction="to_customer",
            destination=desk.customer_telegram_id, body=await public_status(session, desk), actor=actor)
