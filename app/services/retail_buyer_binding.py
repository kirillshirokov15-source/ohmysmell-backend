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
from app.models.customer import Customer
from app.models.draft_order import DraftOrder
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
            existing = await session.scalar(select(Customer.id).where(
                Customer.moysklad_counterparty_id == record["id"],
                Customer.id != order.customer_id))
            if existing is not None:
                raise DeskError("Этот контрагент уже принадлежит другому клиенту CRM. "
                                "Нужно отдельно сверить и объединить клиентские карточки.")
            customer = await session.get(Customer, order.customer_id, with_for_update=True)
            if not customer:
                raise DeskError("CRM-клиент не найден.")
            if customer.moysklad_counterparty_id not in (None, record["id"]):
                raise DeskError("CRM-клиент уже связан с другим контрагентом.")
            draft = await session.get(DraftOrder, desk.draft_id, with_for_update=True)
            if not draft or draft.finalized_order_id != order.id:
                raise DeskError("Не совпадает исходная заявка Tilda.")
            if draft.counterparty_id not in (None, record["id"]):
                raise DeskError("В заявке уже выбран другой контрагент.")
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
                       "evidence": evidence}))
            session.add(DeskEvent(draft_id=desk.draft_id, actor_telegram_id=actor,
                action="buyer_bound", details={"order_id":order.id, "evidence":evidence}))
            # No DeskMessage and no status_notification: internal bookkeeping only.
        return "Покупатель сопоставлен с МойСклад. Можно создавать резерв."

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
