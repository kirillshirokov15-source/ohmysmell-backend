"""Private Telegram intake. Only the transport supplies the authenticated user ID.
Unverified free-text contacts never grant access to an existing profile.
"""
import logging
import re
from datetime import datetime, timezone
from sqlalchemy import select
from app.database.session import async_session
from app.models.customer import Customer, CustomerIdentity
from app.models.draft_order import DraftOrder, DraftOrderItem
from app.models.inbound_message import InboundMessage
from app.models.notification import DraftNotification
from app.models.operations import ClientConversation, ClientUpdate
from app.models.order import Order
from app.services.checkout_service import transaction_lock
from app.services.customer_resolution_service import CustomerResolutionService
from app.models.sales import CustomerIdentityType
from app.logging_utils import log_event

logger = logging.getLogger(__name__)
MENU = "Заявки OhMySmell\n/request — новая заявка\n/status НОМЕР — статус своей заявки\n/manager — связаться с менеджером\n/cancel — отменить ввод\nЦены и наличие подтвердит менеджер."


def parse_items(value):
    rows = [row.strip() for row in value.splitlines() if row.strip()]
    if not 1 <= len(rows) <= 30:
        raise ValueError("Укажите от 1 до 30 позиций, каждую с новой строки.")
    items = []
    for row in rows:
        parts = row.rsplit(";", 1)
        if len(parts) != 2 or not parts[1].strip().isascii() or not parts[1].strip().isdigit():
            raise ValueError("Формат каждой строки: название или артикул; количество. Например: Свеча Лес; 2")
        name, qty = parts[0].strip(), int(parts[1])
        if not 1 <= len(name) <= 300 or not 1 <= qty <= 10000:
            raise ValueError("Название: 1–300 символов; количество: целое число от 1 до 10000.")
        items.append({"name": name, "qty": qty})
    return items


class ClientChannel:
    async def handle(self, telegram_id: int, message_id: int, text: str, *, name="Клиент Telegram", verified_phone=None):
        if telegram_id <= 0 or message_id <= 0:
            raise ValueError("Invalid Telegram envelope")
        key = f"client:{telegram_id}:{message_id}"
        async with async_session() as session, session.begin():
            await transaction_lock(session, f"client:{telegram_id}")
            previous = await session.get(ClientUpdate, key)
            if previous:
                return previous.response
            conversation = await session.get(ClientConversation, telegram_id)
            if not conversation:
                conversation = ClientConversation(telegram_id=telegram_id, state="idle", data={})
                session.add(conversation)
            if conversation.updated_at and (datetime.now(timezone.utc) - conversation.updated_at).total_seconds() > 86400:
                conversation.state, conversation.data = "idle", {}
            try:
                response = await self._handle(session, conversation, key, text.strip(), name[:255], verified_phone)
            except ValueError as error:
                response = str(error)
            conversation.updated_at = datetime.now(timezone.utc)
            session.add(ClientUpdate(key=key, response=response[:4000]))
        return response[:4000]

    async def _handle(self, session, conversation, key, text, name, verified_phone):
        tid = conversation.telegram_id
        if text in {"/start", "/help"}:
            return MENU
        if text == "/cancel":
            conversation.state, conversation.data = "idle", {}
            return "Ввод отменён. " + MENU
        if text == "/request":
            conversation.state, conversation.data = "items", {}
            return "Укажите товары: название или артикул; количество. Каждая позиция — с новой строки.\nНапример: Свеча Лес; 2"
        if text.startswith("/status"):
            try:
                draft_id = int(text.split()[1])
            except (IndexError, ValueError):
                return "Формат: /status НОМЕР_ЗАЯВКИ"
            draft = await session.get(DraftOrder, draft_id)
            # Ownership is the original verified sender, not a supplied phone/email.
            if not draft or draft.source != "telegram" or (draft.contact_details or {}).get("telegram") != str(tid):
                return "Заявка не найдена среди ваших заявок."
            if draft.finalized_order_id:
                order = await session.get(Order, draft.finalized_order_id)
                from app.services.manager_workspace import FULFILLMENT_LABELS, PAYMENT_LABELS
                return f"Заявка №{draft.id}, заказ №{order.id}: {FULFILLMENT_LABELS[order.fulfillment_status]}. {PAYMENT_LABELS[order.payment_status]}."
            return f"Заявка №{draft.id}: " + ("отклонена; свяжитесь с менеджером." if draft.status == "rejected" else "менеджер проверяет товары, наличие и условия.")
        if text == "/manager":
            drafts = list((await session.execute(select(DraftOrder).where(DraftOrder.source == "telegram",
                DraftOrder.contact_details["telegram"].as_string() == str(tid))
                .order_by(DraftOrder.id.desc()).limit(1))).scalars())
            own = next((d for d in drafts if (d.contact_details or {}).get("telegram") == str(tid)), None)
            if not own:
                return "Создайте /request и укажите контакт. Менеджер свяжется с вами по заявке."
            notice = (await session.execute(select(DraftNotification).where(DraftNotification.draft_id == own.id).with_for_update())).scalar_one_or_none()
            if notice:
                notice.status, notice.available_at = "pending", datetime.now(timezone.utc)
            return f"Менеджеру передана просьба связаться с вами по заявке №{own.id}."
        if text.startswith("/") and text != "/send":
            return MENU
        if conversation.state == "items":
            conversation.data = {"items": parse_items(text)}
            conversation.state = "contact"
            return "Поделитесь своим контактом кнопкой или введите телефон/email. Контакт, введённый текстом, проверит менеджер."
        if conversation.state == "contact":
            if not verified_phone and (len(text) > 320 or not ("@" in text or re.fullmatch(r"[+\d\s()\-]{10,30}", text))):
                return "Введите телефон/email или отправьте свой контакт."
            data = dict(conversation.data)
            data["contact"] = verified_phone or text
            data["verified_phone"] = verified_phone
            conversation.data, conversation.state = data, "confirm"
            return "Проверьте заявку:\n" + "\n".join(f"{i['name']} × {i['qty']}" for i in data["items"])[:3000] + "\n/send — отправить; /cancel — отменить. Цену подтвердит менеджер."
        if conversation.state == "confirm" and text == "/send":
            return await self._submit(session, conversation, key, name)
        return "Для новой заявки: /request. Для отправки подготовленной: /send."

    async def _submit(self, session, conversation, key, name):
        data, tid = conversation.data, conversation.telegram_id
        identities = [("telegram", str(tid))]
        if data.get("verified_phone"):
            phone = CustomerResolutionService.normalize(CustomerIdentityType.PHONE, data["verified_phone"])
            identities.append(("phone", phone))
        claimed = None
        if not data.get("verified_phone"):
            kind = "email" if "@" in data["contact"] else "phone"
            value = CustomerResolutionService.normalize(CustomerIdentityType(kind), data["contact"])
            claimed = (kind, value)
        for kind, value in sorted(identities + ([claimed] if claimed else [])):
            await transaction_lock(session, f"identity:{kind}:{value}")
        from sqlalchemy import tuple_
        customers = list((await session.execute(select(Customer).join(CustomerIdentity).where(
            tuple_(CustomerIdentity.identity_type, CustomerIdentity.normalized_value).in_(identities)))).scalars().unique())
        if len(customers) > 1:
            return "Контакты относятся к разным профилям. Отправьте контакт текстом для проверки менеджером: /request."
        # An unverified contact can route a request to a known profile, like the
        # website intake. It cannot attach a new identity or expose profile data.
        claimed_customer = None
        if not customers and claimed:
            claimed_customer = (await session.execute(select(Customer).join(CustomerIdentity).where(
                CustomerIdentity.identity_type == claimed[0], CustomerIdentity.normalized_value == claimed[1]))).scalar_one_or_none()
        if claimed_customer:
            customers = [claimed_customer]
        customer = customers[0] if customers else Customer(customer_type="unknown", display_name=name)
        if not customers:
            session.add(customer)
            await session.flush()
        for kind, value in ([] if claimed_customer else identities):
            existing = (await session.execute(select(CustomerIdentity).where(CustomerIdentity.identity_type == kind,
                CustomerIdentity.normalized_value == value))).scalar_one_or_none()
            if not existing:
                session.add(CustomerIdentity(customer_id=customer.id, identity_type=kind, normalized_value=value, original_value=value))
        conversation.customer_id = customer.id
        inbound = InboundMessage(source="telegram", external_message_id=key, sender=str(tid),
            subject="Заявка Telegram", body_text="\n".join(f"{i['name']}; {i['qty']}" for i in data["items"]),
            received_at=datetime.now(timezone.utc), processing_status="processed", customer_id=customer.id)
        session.add(inbound)
        await session.flush()
        draft = DraftOrder(inbound_message_id=inbound.id, customer_id=customer.id, source="telegram",
            customer_type=customer.customer_type, sender_email="", customer_name=name, subject="Заявка Telegram",
            status="needs_review", counterparty_id=customer.moysklad_counterparty_id, counterparty_candidates=[],
            contact_details={"telegram": str(tid), "phone": data["contact"] if "@" not in data["contact"] else "", "contact_verified": bool(data.get("verified_phone"))},
            review_notes="Подтвердите контакт, тип клиента, товары, цены и наличие.")
        for item in data["items"]:
            draft.items.append(DraftOrderItem(raw_product_text=item["name"], qty=item["qty"], match_status="not_found", candidates=[]))
        session.add(draft)
        await session.flush()
        session.add(DraftNotification(draft_id=draft.id))
        conversation.state, conversation.data = "idle", {}
        log_event(logger, "draft_created", draft_id=draft.id, action="telegram_intake", result="pending_commit")
        return f"Заявка №{draft.id} принята. Менеджер подтвердит цену и наличие.\n/status {draft.id} — статус; /manager — связь с менеджером."
