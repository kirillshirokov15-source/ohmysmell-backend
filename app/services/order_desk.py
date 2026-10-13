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
from app.integrations.tilda import digest
from app.services.tilda_review import (review, apply_review, source_rows, confirmation_problems, ITEM_FIELDS)
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


async def client_navigation_desks(session, actor):
    """Navigation has a real owned order context; never bypass outbox scope.

With a staging draft scope, neither the list nor its keyboard may borrow an
approved draft's envelope to show orders outside that scope. Message-ID-only
approval still requires explicit approval of the individual outbox row.
"""
    from app.services.desk_delivery_policy import DeskDeliveryPolicy
    policy = DeskDeliveryPolicy.load("client")
    query = select(OrderDesk).where(OrderDesk.customer_telegram_id == actor)
    if policy.restricted and policy.draft_ids:
        query = query.where(OrderDesk.draft_id.in_(policy.draft_ids))
    return (await session.scalars(query.order_by(OrderDesk.draft_id.desc()).limit(10))).all()


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
            rows, total, problems = await review(payload)
            draft = DraftOrder(inbound_message_id=inbound.id, customer_id=customer.id, source="website",
                customer_type="retail", customer_name=payload["name"] or "Клиент сайта", sender_email=email,
                subject="Заказ Tilda", status="needs_review", counterparty_candidates=[],
                contact_details={"phone": payload["phone"], "comment": payload["comment"],
                    "tilda": True, "reported_total_minor": payload["reported_total"],
                    "discount_minor": payload["discount"], "currency": payload["currency"],
                    "external_products": [i["externalid"] for i in payload["items"]]})
            for row in payload["items"]:
                draft.items.append(DraftOrderItem(raw_product_text=row["name"], qty=row["qty"]))
            apply_review(draft, rows, total, problems)
            session.add(draft)
            await session.flush()
            draft.contact_details = {**draft.contact_details,
                "tilda_item_sources": {str(item.id): n for n, item in enumerate(draft.items)}}
            desk = OrderDesk(draft_id=draft.id, external_id=payload["external_id"], payload_hash=fingerprint,
                             stage="awaiting_confirmation")
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
            draft = await session.get(DraftOrder, draft_id)
            if not draft or draft.status == "rejected" or desk.order_id:
                raise DeskError("Заявка закрыта для назначения.")
            desk.manager_id, desk.actor_telegram_id, desk.assigned_at = manager.id, actor, now()
            desk.stage = "awaiting_confirmation"
            audit(session, desk, "manager_claim_succeeded", actor)
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
            destination=manager_chat(), body=f"К заявке №{desk.draft_id} подключён клиент Telegram.")
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
            reply = enqueue(session, draft_id=draft_id, key=key + ":ack", direction="to_customer",
                destination=actor, body=response, kind="client_menu", source_message_id=message_id)
            await session.flush()
            from app.services.desk_delivery_policy import DeskDeliveryPolicy
            delivery = ("sending_disabled" if os.getenv("ORDER_DESK_SEND_ENABLED", "false").lower() != "true"
                        else "scope_allowed" if DeskDeliveryPolicy.load("client").permits(reply) else "scope_blocked")
            log_event(logger, "client_reply_queued", message_id=reply.id, draft_id=draft_id, result=delivery)

    async def _client(self, session, actor, message_id, text, key):
        from app.models.operations import ClientConversation
        conversation = await session.get(ClientConversation, actor)
        if not conversation:
            conversation = ClientConversation(telegram_id=actor, state="idle", data={})
            session.add(conversation)
        if text is None:
            return "Отправьте текст сообщения. Вложения передайте менеджеру напрямую.", None
        text = text.strip()
        if text.startswith("/"):
            conversation.state, conversation.data = "idle", {}
        if text.startswith("/start "):
            desk = await self.consume(session, text.split(maxsplit=1)[1], actor)
            conversation.state, conversation.data = "idle", {}
            return "Спасибо за заказ!\n" + await public_status(session, desk), desk.draft_id
        if text in {"/start", "/help", "/cancel"}:
            conversation.state, conversation.data = "idle", {}
            desks = await client_navigation_desks(session, actor)
            return ("Здесь можно посмотреть свои заказы и написать менеджеру. Выберите действие в меню.",
                    desks[0].draft_id if desks else None)
        if text in {"/orders", "Мой заказ", "📦 Мои заказы", "🔎 Статус заказа", "💬 Написать менеджеру", "/status", "/manager"}:
            conversation.state = "choose_message" if text in {"💬 Написать менеджеру", "/manager"} else "choose_status"
            conversation.data = {}
            desks = await client_navigation_desks(session, actor)
            return ("Выберите заказ:\n" + "\n".join([await public_status(session, d) for d in desks])
                if desks else "Заказов пока нет. Откройте персональную ссылку, полученную после оформления заказа.",
                desks[0].draft_id if desks else None)
        if text.startswith(("Заказ №", "Заявка №")):
            number = text.split("№", 1)[1]
            if not number.isascii() or not number.isdigit() or len(number) > 10 or not 0 < int(number) <= 2147483647:
                raise DeskError("Выберите заказ кнопкой под сообщением.")
            identifier = int(number)
            if text.startswith("Заказ №"):
                desk = await session.scalar(select(OrderDesk).where(OrderDesk.order_id == identifier,
                    OrderDesk.customer_telegram_id == actor))
            else:
                desk = await session.get(OrderDesk, identifier)
            if not desk or desk.customer_telegram_id != actor:
                raise DeskError("Заказ не найден среди ваших заказов.")
            identifier = desk.draft_id
            if conversation.state == "choose_message":
                conversation.state, conversation.data = "desk_message", {"draft_id": identifier}
                return f"Напишите сообщение менеджеру по заявке №{identifier}.", identifier
            conversation.state, conversation.data = "idle", {}
            return await public_status(session, desk), identifier
        if conversation.state == "desk_message" and not text.startswith("/"):
            text = f"/message {conversation.data['draft_id']} {text}"
        if not text:
            return "Выберите действие в меню.", None
        parts = text.split(maxsplit=2)
        if parts[0] not in {"/status", "/message", "/manager"} or len(parts) < 2 or not parts[1].isascii() or not parts[1].isdigit() or len(parts[1]) > 9:
            return "Выберите заказ через меню, чтобы посмотреть статус или написать менеджеру.", None
        desk = await session.get(OrderDesk, int(parts[1]))
        if not desk or desk.customer_telegram_id != actor:
            raise DeskError("Заявка не найдена среди ваших заказов.")
        if parts[0] == "/status":
            return await public_status(session, desk), desk.draft_id
        if len(parts) != 3 or not parts[2].strip() or len(parts[2].encode("utf-16-le")) // 2 > 3000:
            raise DeskError("Введите сообщение длиной до 3000 символов.")
        enqueue(session, draft_id=desk.draft_id, key=key, direction="to_manager", destination=manager_chat(),
            body=f"Сообщение от клиента по заявке №{desk.draft_id}:\n{parts[2]}",
            sender_type="customer", actor=actor, source_message_id=message_id)
        audit(session, desk, "customer_message_received", actor)
        conversation.state, conversation.data = "idle", {}
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
            # Match OrderOperations' lock order: Order before Desk. Recheck after
            # the lock in case confirmation completed while this card was open.
            order_id = await session.scalar(select(OrderDesk.order_id).where(OrderDesk.draft_id == draft_id))
            order = await session.get(Order, order_id, with_for_update=True) if order_id else None
            desk = await session.get(OrderDesk, draft_id, with_for_update=True)
            if not desk:
                raise DeskError("Заявка не найдена.")
            require_owner(desk, actor)
            if desk.order_id != order_id:
                raise DeskError("Карточка изменилась. Обновите заявку.")
            if desk.order_id:
                if stage != "awaiting_payment":
                    raise DeskError("Заказ уже подтверждён. Используйте карточку заказа.")
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
                        destination=desk.customer_telegram_id, body=await public_status(session, desk))

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
                inbound = await session.get(InboundMessage, draft.inbound_message_id)
                try:
                    payload, items, _ = source_rows(draft, desk, inbound)
                except (ValueError, KeyError, TypeError) as error:
                    raise DeskError("Исходные позиции требуют проверки. Выполните /sitereprice.") from error
                if confirmation_problems(draft, payload, items):
                    raise DeskError("Товары, цены, скидка или сумма требуют проверки. Выполните /sitereprice.")
                # Revalidate identity against a fresh (TTL-bounded) catalog. Never
                # silently replace a product or approve an expired match.
                details = draft.contact_details or {}
                checked, _, problems = await review(payload,
                    manual_matches=details.get("tilda_manual_matches"),
                    manual_total=details.get("tilda_manual_total"))
                if problems or any(i.product_id != r["product_id"] for i, r in zip(items, checked, strict=True)):
                    raise DeskError("Каталог недоступен или сопоставление изменилось. Выполните /sitereprice.")
                draft.status = "ready"
                draft.contact_details = {**draft.contact_details, "tilda_review_approved": True}
                audit(session, desk, "tilda_review_approved", actor, total_minor=draft.total, revision=revision)
            from app.repositories.draft_order_repository import DraftOrderRepository
            return await DraftOrderRepository().finalize(draft_id, session=session)

    async def set_manual_product(self, draft_id, item_id, product_id, actor, chat_id):
        """Explicit owner choice, bound to immutable Webhook line and live catalog."""
        from app.services.product_matching_service import ProductMatchingService
        async with async_session() as session, session.begin():
            await authorized_manager(session, actor, chat_id)
            draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id)
                .options(selectinload(DraftOrder.items)).with_for_update())
            desk = await session.get(OrderDesk, draft_id, with_for_update=True)
            if not draft or not desk or desk.order_id or draft.status == "rejected":
                raise DeskError("Заявка уже закрыта или подтверждена.")
            require_owner(desk, actor)
            inbound = await session.get(InboundMessage, draft.inbound_message_id)
            payload, items, _ = source_rows(draft, desk, inbound)
            position = next((n for n, item in enumerate(items) if item.id == item_id), None)
            if position is None:
                raise DeskError("Позиция не относится к этой заявке.")
            products = await ProductMatchingService().products_async()
            found = [p for p in products if p.get("id") == product_id and not p.get("archived")]
            if len(found) != 1:
                raise DeskError("Номенклатура не найдена в актуальном МойСклад.")
            existing = (draft.contact_details or {}).get("tilda_manual_matches") or {}
            override = {"sku": payload["items"][position]["externalid"], "product_id": product_id}
            if existing.get(str(position)) == override:
                return {"changed": False, "revision": draft.revision}
            matches = {**existing, str(position): override}
            rows, total, problems = await review(payload, manual_matches=matches,
                manual_total=(draft.contact_details or {}).get("tilda_manual_total"))
            if rows[position]["product_id"] != product_id or rows[position]["match_status"] != "matched":
                raise DeskError("Не удалось подтвердить выбранный товар по свежему каталогу.")
            apply_review(draft, rows, total, problems, items)
            draft.contact_details = {**draft.contact_details, "tilda_manual_matches": matches}
            draft.revision += 1
            audit(session, desk, "tilda_manual_product_matched", actor,
                item_id=item_id, product_id=product_id, source_sku=payload["items"][position]["externalid"])
            return {"changed": True, "revision": draft.revision}

    async def approve_missing_total(self, draft_id, revision, actor, chat_id):
        """Manager-approved sum only for missing overall site total, never mismatches."""
        from app.services.tilda_review import amounts, digest
        async with async_session() as session, session.begin():
            await authorized_manager(session, actor, chat_id)
            draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id)
                .options(selectinload(DraftOrder.items)).with_for_update())
            desk = await session.get(OrderDesk, draft_id, with_for_update=True)
            if not draft or not desk or desk.order_id or draft.status == "rejected":
                raise DeskError("Заявка закрыта для согласования.")
            require_owner(desk, actor)
            if draft.revision != revision:
                raise DeskError("Карточка изменилась. Обновите заявку.")
            inbound = await session.get(InboundMessage, draft.inbound_message_id)
            payload, items, _ = source_rows(draft, desk, inbound)
            raw_problems, _ = amounts(payload)
            if (payload.get("reported_total") is not None or payload.get("discount") != 0
                    or set(raw_problems) != {"invalid_total", "total_mismatch"}):
                raise DeskError("Ручное согласование разрешено только когда Tilda вообще не передала итог, "
                                "а цены позиций и скидка корректны.")
            subtotal = sum(row["price_minor"] * row["qty"] for row in payload["items"])
            if not 0 < subtotal <= 100_000_000_000:
                raise DeskError("Сумма позиций недоступна.")
            attestation = {"digest": digest(payload), "amount_minor": subtotal}
            details = draft.contact_details or {}
            rows, total, problems = await review(payload,
                manual_matches=details.get("tilda_manual_matches"), manual_total=attestation)
            if problems or total != subtotal or any(row["match_status"] != "matched" for row in rows):
                raise DeskError("Сначала сопоставьте все товары с МойСклад и устраните остальные ошибки.")
            apply_review(draft, rows, total, problems, items)
            draft.contact_details = {**draft.contact_details,
                "tilda_manual_total": attestation,
                "tilda_manual_total_approved_by": actor}
            draft.revision += 1
            audit(session, desk, "tilda_manual_total_approved", actor, total_minor=subtotal)
            return {"total_minor": subtotal, "revision": draft.revision}

    async def reprice(self, draft_id, actor, chat_id, *, dry_run=False):
        async with async_session() as session, session.begin():
            await authorized_manager(session, actor, chat_id)
            draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id)
                .options(selectinload(DraftOrder.items)).with_for_update())
            desk = await session.get(OrderDesk, draft_id, with_for_update=True)
            if (not desk or not draft or desk.order_id or draft.finalized_order_id
                    or draft.status in {"rejected", "new"} or draft.source != "website"):
                raise DeskError("Заявка недоступна.")
            require_owner(desk, actor)
            inbound = await session.get(InboundMessage, draft.inbound_message_id)
            try:
                payload, items, bindings = source_rows(draft, desk, inbound)
            except (ValueError, KeyError, TypeError) as error:
                raise DeskError("Нельзя безопасно восстановить исходные позиции.") from error
            details = draft.contact_details or {}
            rows, total, problems = await review(payload,
                manual_matches=details.get("tilda_manual_matches"),
                manual_total=details.get("tilda_manual_total"))
            from app.services.tilda_review import REVIEW_NOTE
            changed = (any(any(getattr(i, key) != row[key] for key in ITEM_FIELDS)
                           for i, row in zip(items, rows, strict=True))
                or draft.total != total or draft.review_notes != (", ".join(problems) or REVIEW_NOTE)
                or draft.status != "needs_review" or draft.contact_details.get("tilda_review_approved") is not False
                or draft.contact_details.get("tilda_item_sources") != bindings
                or draft.contact_details.get("tilda_review_problems") != problems
                or draft.contact_details.get("tilda_price_source") != "webhook")
            result = {"draft_id": draft.id, "revision": draft.revision, "changed": changed,
                      "total_minor": total, "problems": problems,
                      "items": [{"item_id": i.id, "sku": r["externalid"], **row}
                                for i, r, row in zip(items, payload["items"], rows, strict=True)]}
            if dry_run:
                return result
            if changed:
                apply_review(draft, rows, total, problems, items)
                draft.contact_details = {**draft.contact_details, "tilda_item_sources": bindings}
                draft.revision += 1
            audit(session, desk, "tilda_repriced", actor, revision=draft.revision, changed=changed)
            result["revision"] = draft.revision
            return result


async def public_status(session, desk):
    from app.services.desk_status import public_summary
    draft = await session.get(DraftOrder, desk.draft_id)
    order = await session.get(Order, desk.order_id) if desk.order_id else None
    return public_summary(desk, draft, order)


async def status_notification(session, order, actor):
    desk = await session.scalar(select(OrderDesk).where(OrderDesk.order_id == order.id))
    if desk:
        audit(session, desk, "order_status_changed", actor, order_id=order.id, revision=order.revision,
            payment=order.payment_status, fulfillment=order.fulfillment_status, delivery=order.delivery_status)
    if desk and desk.customer_telegram_id:
        body = await public_status(session, desk)
        previous = await session.scalar(select(DeskMessage).where(DeskMessage.draft_id == desk.draft_id,
            DeskMessage.destination == desk.customer_telegram_id, DeskMessage.kind == "client_card")
            .order_by(DeskMessage.id.desc()).limit(1))
        # Preserve separate important events; intermediate assembly updates only edit the card.
        important = (previous is None or order.needs_review or
            order.fulfillment_status in {"shipped", "cancelled"} or order.delivery_status == "delivered"
            or (previous.body.splitlines()[1:] != body.splitlines()[1:]))
        if important and previous is not None and previous.body != body:
            enqueue(session, draft_id=desk.draft_id, key=f"status:{order.id}:{order.revision}:notice",
                direction="to_customer", destination=desk.customer_telegram_id, body=body)
        enqueue(session, draft_id=desk.draft_id, key=f"status:{order.id}:{order.revision}", direction="to_customer",
            destination=desk.customer_telegram_id, body=await public_status(session, desk), actor=actor, kind="client_card")
