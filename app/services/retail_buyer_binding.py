"""Manager-confirmed retail (Tilda) buyer binding for already finalised orders.

Provider searches do not mutate inventory or CRM records. Only an assigned
manager may bind a verified, non-archived counterparty or explicitly create a
new one after a fresh duplicate check. No customer notifications are queued.
"""
import hashlib
from sqlalchemy import select
from app.config.settings import settings
from app.database.session import async_session
from app.models.order import Order
from app.models.customer import Customer, CustomerIdentity
from app.models.draft_order import DraftOrder
from app.models.inbound_message import InboundMessage
from app.models.order_desk import OrderDesk, DeskEvent
from app.models.operations import OrderEvent
from app.services.order_desk import DeskError, authorized_manager, require_owner
from app.services.counterparty_matching_service import (
    CounterpartyMatchingService, emails, normalize_phone, normalize_text,
)


def verified_identity(provider, *, email=None, phone=None, name=None):
    """A single first name is never proof of an existing customer identity."""
    if provider.get("archived") or not provider.get("id"):
        return None
    email_values = emails(email)
    if email_values and email_values.intersection(emails(provider.get("email"))):
        return "email"
    if phone and len(normalize_phone(phone)) >= 10 and (
        normalize_phone(phone) == normalize_phone(provider.get("phone"))
    ):
        return "phone"
    full = normalize_text(name)
    if len(full.split()) >= 2 and len(full) >= 6 and full == normalize_text(provider.get("name")):
        return "full_name"
    return None


async def reusable_retail_customer(session, existing_customer_id, current, provider_id, evidence):
    """Find the existing CRM customer only with strong independent contact proof.

    Never merge users on a matching name alone. The old owner must have a
    previous *confirmed* retail order for the exact MoySklad counterparty,
    sharing a phone or email with the new website order. No customer records
    are deleted, no order history is rewritten.
    """
    if evidence not in {"email", "phone"}:
        raise DeskError(
            "Этот контрагент уже принадлежит другой карточке CRM. "
            "Для объединения недостаточно совпадения имени; нужны одинаковые "
            "email или телефон предыдущего заказа.")
    old = await session.get(Customer, existing_customer_id, with_for_update=True)
    if not old or old.customer_type not in {"retail", "unknown"}:
        raise DeskError("Существующая карточка CRM другого типа – требуется ручная сверка.")
    own_email = emails(current.get("email"))
    own_phone = normalize_phone(current.get("phone"))
    previous = (await session.scalars(select(Order).where(
        Order.customer_id == existing_customer_id,
        Order.counterparty_id == provider_id,
        Order.source == "website",
        Order.fulfillment_status != "cancelled",
        Order.status != "rejected",
    ).order_by(Order.id.desc()).limit(30))).all()
    matches = any(
        bool(own_email and own_email.intersection(emails(old_order.customer_email)))
        or bool(own_phone and len(own_phone) >= 10
                and own_phone == normalize_phone(old_order.phone))
        for old_order in previous
    )
    if not matches:
        raise DeskError(
            "Контрагент привязан к другому клиенту CRM, но телефон/email "
            "его прошлых заказов не совпадают. Автоматическое объединение запрещено.")
    # Only an isolated customer automatically created for the current website
    # intake can be reparented. Reusing an established profile could expose
    # someone else's orders through connected CRM/customer channels.
    provisional_id = current["customer_id"]
    other_order = await session.scalar(select(Order.id).where(
        Order.customer_id == provisional_id, Order.id != current["order_id"]).limit(1))
    other_draft = await session.scalar(select(DraftOrder.id).where(
        DraftOrder.customer_id == provisional_id,
        DraftOrder.finalized_order_id != current["order_id"]).limit(1))
    other_identity = await session.scalar(select(CustomerIdentity.id).where(
        CustomerIdentity.customer_id == provisional_id).limit(1))
    if other_order is not None or other_draft is not None or other_identity is not None:
        raise DeskError("У новой карточки CRM есть другая история или идентификаторы. "
                        "Автоматическое объединение запрещено.")
    return old


class RetailBuyerService:
    def __init__(self, matcher=None, operations=None, sessions=None):
        self.matcher = matcher or CounterpartyMatchingService()
        self.operations = operations
        self.sessions = sessions or async_session

    async def _read(self, order_id, actor, chat_id):
        async with self.sessions() as session:
            await authorized_manager(session, actor, chat_id)
            order = await session.get(Order, order_id)
            desk = await session.scalar(select(OrderDesk).where(OrderDesk.order_id == order_id))
            if not order or not desk or not order.manual_fulfillment or order.source != "website":
                raise DeskError("Доступно только для подтверждённых заказов Tilda.")
            require_owner(desk, actor)
            if order.status == "rejected" or order.fulfillment_status in {"cancelled", "shipped"}:
                raise DeskError("Для закрытого заказа изменение контрагента запрещено.")
            return dict(order_id=order.id, revision=order.revision,
                        customer_id=order.customer_id, name=order.customer_name or "",
                        email=str(order.customer_email or ""), phone=order.phone or "",
                        counterparty_id=order.counterparty_id)

    async def candidates(self, order_id, actor, chat_id, query=None):
        data = await self._read(order_id, actor, chat_id)
        if data["counterparty_id"]:
            raise DeskError("Покупатель уже сопоставлен. Проверьте карточку заказа.")
        if query:
            value = " ".join(query.split())
            if not 3 <= len(value) <= 150:
                raise DeskError("Введите минимум 3 и максимум 150 символов для поиска.")
            candidates = await self.matcher.manual_search_async(value)
        else:
            candidates = await self.matcher.candidates_async(
                data["email"], data["phone"], data["name"])
        return data, candidates[:5]

    async def bind(self, order_id, actor, chat_id, counterparty_id, expected_revision):
        from app.integrations.moysklad.client import MoySkladClient
        from app.integrations.moysklad.async_gateway import AsyncMoySkladGateway
        data = await self._read(order_id, actor, chat_id)
        if data["revision"] != expected_revision:
            raise DeskError("Заказ изменился. Откройте новую карточку.")
        if data["counterparty_id"]:
            if data["counterparty_id"] == counterparty_id:
                return "Контрагент уже привязан."
            raise DeskError("Контрагент заказа уже выбран – требуется отдельная сверка.")
        record = await self.matcher.async_gateway.get_counterparty(counterparty_id)
        if record.get("id") != counterparty_id or record.get("archived"):
            raise DeskError("Контрагент МойСклад не найден или архивирован.")
        evidence = verified_identity(record, email=data["email"],
                                     phone=data["phone"], name=data["name"])
        if not evidence:
            raise DeskError("Совпадение с покупателем не подтверждено по email, "
                            "телефону или полному ФИО. Привязка запрещена.")
        return await self._persist(data, actor, chat_id, record, evidence)

    async def _persist(self, original, actor, chat_id, record, evidence):
        from app.services.checkout_service import transaction_lock
        from app.models.manager import Manager
        async with self.sessions() as session, session.begin():
            manager = await authorized_manager(session, actor, chat_id)
            await transaction_lock(session, f"retail-buyer:{original['order_id']}")
            await transaction_lock(session, f"retail-buyer-counterparty:{record['id']}")
            order = await session.scalar(
                select(Order).where(Order.id == original["order_id"]).with_for_update())
            desk = await session.scalar(select(OrderDesk).where(
                OrderDesk.order_id == original["order_id"]).with_for_update())
            if not order or not desk:
                raise DeskError("Заказ больше не найден.")
            require_owner(desk, actor)
            if order.status == "rejected" or order.fulfillment_status in {"cancelled", "shipped"}:
                raise DeskError("Заказ закрыт; привязка недоступна.")
            if order.counterparty_id:
                if order.counterparty_id == record["id"]:
                    return "Контрагент уже привязан."
                raise DeskError("Контрагент заказа изменён; обновите карточку.")
            if order.revision != original["revision"]:
                raise DeskError("Заказ изменился – обновите карточку.")
            if not order.customer_id:
                raise DeskError("У заказа отсутствует CRM-клиент.")
            previous_customer_id = order.customer_id
            customer = await session.get(Customer, previous_customer_id, with_for_update=True)
            if not customer:
                raise DeskError("CRM-клиент не найден.")
            if customer.moysklad_counterparty_id not in (None, record["id"]):
                raise DeskError("CRM-клиент уже связан с другим контрагентом.")
            existing = await session.scalar(select(Customer.id).where(
                Customer.moysklad_counterparty_id == record["id"],
                Customer.id != previous_customer_id))
            canonical = None
            if existing is not None:
                canonical = await reusable_retail_customer(
                    session, existing,
                    {**original, "customer_id": previous_customer_id}, record["id"], evidence)
            draft = await session.get(DraftOrder, desk.draft_id, with_for_update=True)
            if not draft or draft.finalized_order_id != order.id:
                raise DeskError("Не совпадает исходная заявка Tilda.")
            if draft.counterparty_id not in (None, record["id"]):
                raise DeskError("В заявке уже выбран другой контрагент.")
            if draft.customer_id != previous_customer_id:
                raise DeskError("Клиент заявки уже изменился. Обновите карточку.")
            if canonical:
                inbound = await session.get(InboundMessage, draft.inbound_message_id, with_for_update=True)
                if not inbound or inbound.source != "website" or inbound.customer_id != previous_customer_id:
                    raise DeskError("Исходное обращение Tilda не соответствует клиенту.")
                if (await session.scalar(select(InboundMessage.id).where(
                    InboundMessage.customer_id == previous_customer_id,
                    InboundMessage.id != inbound.id).limit(1))) is not None:
                    raise DeskError("У клиента CRM есть другие обращения. Нужна ручная сверка.")
                # Update only the current order, its draft and inbound; retain
                # the canonical customer's entire history and identities.
                order.customer_id = draft.customer_id = inbound.customer_id = canonical.id
            else:
                customer.moysklad_counterparty_id = record["id"]
            draft.counterparty_id, draft.counterparty_name = record["id"], record.get("name")
            order.counterparty_id, order.counterparty_name = record["id"], record.get("name")
            order.revision += 1
            key = f"buyer:order:{order.id}:{record['id']}"
            session.add(OrderEvent(order_id=order.id, manager_id=manager.id,
                action="buyer_bound", idempotency_key=key,
                request_hash=hashlib.sha256(key.encode()).hexdigest(),
                before={"counterparty_id": None},
                after={"counterparty_id": record["id"], "revision": order.revision,
                       "evidence": evidence, "customer_id": order.customer_id,
                       "reused_customer": bool(canonical)}))
            session.add(DeskEvent(draft_id=desk.draft_id, actor_telegram_id=actor,
                action="buyer_bound", details={"order_id":order.id, "evidence":evidence,
                    "reused_customer":bool(canonical),
                    "previous_customer_id":previous_customer_id,
                    "current_customer_id":order.customer_id}))
            # No DeskMessage and no status_notification: internal bookkeeping only.
        return ("Покупатель сопоставлен с МойСклад; повторная карточка CRM не создавалась. "
                "Можно создавать резерв." if existing is not None
                else "Покупатель сопоставлен с МойСклад. Можно создавать резерв.")

    async def preview_new(self, order_id, actor, chat_id, full_name):
        from app.services.counterparty_creation_service import CounterpartyCreationService
        data = await self._read(order_id, actor, chat_id)
        if data["counterparty_id"]:
            raise DeskError("Контрагент уже сопоставлен.")
        try:
            name = CounterpartyCreationService.clean_name(full_name)
        except ValueError as error:
            raise DeskError(str(error)) from error
        if len(normalize_text(name).split()) < 2:
            raise DeskError("Укажите полное имя и фамилию, чтобы избежать дублей.")
        found = await self.matcher.duplicate_candidates(
            name=name, email=data["email"], phone=data["phone"])
        if found:
            raise DeskError("В МойСклад обнаружены существующие или похожие покупатели. "
                            "Сначала выберите существующую карточку через поиск.")
        return data, name

    async def create_and_bind(self, order_id, actor, chat_id, full_name, expected_revision):
        from app.integrations.write_guard import require_counterparty_creation
        from app.services.external_operation_service import ExternalOperationService, ExternalOperationConflict
        require_counterparty_creation()
        data = await self._read(order_id, actor, chat_id)
        if data["revision"] != expected_revision or data["counterparty_id"]:
            raise DeskError("Заказ изменился – обновите карточку покупателя.")
        if not data["customer_id"]:
            raise DeskError("У заказа отсутствует CRM-клиент.")
        from app.services.counterparty_creation_service import CounterpartyCreationService
        name = CounterpartyCreationService.clean_name(full_name)
        if len(normalize_text(name).split()) < 2:
            raise DeskError("Укажите полное имя и фамилию.")
        if not data["email"] and not data["phone"]:
            raise DeskError("Для новой карточки нужен email или телефон заказа.")
        key = f"oms:counterparty:customer:{data['customer_id']}"
        operations = self.operations or ExternalOperationService()
        existing = await operations.store.get(key)
        payload = {"name":name, "externalCode":key,
                   "description":"OhMySmell: розничный покупатель из подтверждённого заказа Tilda."}
        if data["email"]:
            payload["email"] = data["email"]
        if data["phone"]:
            payload["phone"] = data["phone"]
        if existing is None:
            await self.preview_new(order_id, actor, chat_id, name)
        elif existing["status"] != "succeeded" or existing["payload"] != payload:
            raise DeskError("Создание контрагента уже выполнялось или требует сверки. "
                            "Повторная запись заблокирована.")
        async def send(document):
            return await self.matcher.async_gateway.create_counterparty(document)
        try:
            created = await operations.execute_counterparty(key, payload, send)
        except ExternalOperationConflict as error:
            raise DeskError("Результат создания контрагента МойСклад требует сверки; "
                            "повторное создание запрещено.") from error
        record = await self.matcher.async_gateway.get_counterparty(created["id"])
        if (record.get("id") != created["id"] or record.get("archived")
                or normalize_text(record.get("name")) != normalize_text(name)):
            raise DeskError("Контрагент создан, но не прошёл проверку. Повторять создание нельзя.")
        return await self._persist(data, actor, chat_id, record, "manager_created")
