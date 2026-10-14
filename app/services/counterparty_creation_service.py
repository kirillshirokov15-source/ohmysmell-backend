"""Explicit, idempotent MoySklad counterparty creation from manager Telegram.

Never create from email alone. A manager first previews the exact organization
name, with a duplicate search, then confirms via revision-scoped callback.
Only counterparty POST is possible (no document, warehouse, Gmail writes).
"""
import re
import time
from app.services.counterparty_matching_service import CounterpartyMatchingService, emails, normalize_text
from app.services.external_operation_service import ExternalOperationService, ExternalOperationConflict
from app.integrations.write_guard import require_counterparty_creation


class CounterpartyCreationError(ValueError):
    pass


class CounterpartyCreationService:
    def __init__(self, matcher=None, operations=None, repository=None):
        self.matcher = matcher or CounterpartyMatchingService()
        self.operations = operations or ExternalOperationService()
        if repository is None:
            from app.repositories.draft_order_repository import DraftOrderRepository
            repository = DraftOrderRepository()
        self.repository = repository

    @staticmethod
    def clean_name(value):
        name = " ".join(str(value or "").strip().split())
        if not 3 <= len(name) <= 180 or "@" in name or any(ord(ch) < 32 for ch in name):
            raise CounterpartyCreationError("Укажите полное название юридического лица/ИП (3–180 символов)")
        if normalize_text(name) in {"контрагент", "новый клиент", "неизвестно", "клиент"}:
            raise CounterpartyCreationError("Нужно реальное наименование контрагента")
        return name

    async def preview(self, draft_id, name, manager_id):
        name = self.clean_name(name)
        draft = await self.repository.get(draft_id)
        if draft is None or draft.source != "email" or draft.counterparty_id:
            raise CounterpartyCreationError("Нужна заявка с почты без привязанного контрагента")
        if (draft.contact_details or {}).get("mail_intent") in {"inquiry", "possible_order"}:
            raise CounterpartyCreationError("Сначала подтвердите, что письмо является заказом")
        # Fail closed when the provider is offline or the index cannot be read.
        conflicts = await self.matcher.duplicate_candidates(name=name, email=draft.sender_email)
        if conflicts:
            choices = "; ".join((row.get("name") or str(row["id"]))[:80] for row in conflicts[:5])
            raise CounterpartyCreationError(
                "Похожие/существующие контрагенты найдены: " + choices +
                ". Создание заблокировано. Используйте /cpsearch НОМЕР НАЗВАНИЕ для выбора.")
        try:
            return await self.repository.prepare_new_counterparty(draft_id, name, manager_id)
        except Exception as error:
            from app.services.order_lifecycle import InvalidOrderTransitionError
            if isinstance(error, InvalidOrderTransitionError):
                raise CounterpartyCreationError(str(error)) from error
            raise

    async def create_confirmed(self, draft_id, manager_id):
        require_counterparty_creation()
        draft = await self.repository.get(draft_id)
        if draft is None or draft.source != "email" or draft.counterparty_id:
            raise CounterpartyCreationError("Нельзя создавать контрагента для этого заказа")
        pending = (draft.contact_details or {}).get("counterparty_create_preview") or {}
        if not pending or pending.get("actor") != manager_id or pending.get("revision") != draft.revision:
            raise CounterpartyCreationError("Подтверждение устарело; повторите /newcp")
        if (draft.contact_details or {}).get("mail_intent") in {"possible_order", "inquiry"}:
            raise CounterpartyCreationError("Сначала классифицируйте письмо как заказ")
        if not isinstance(pending.get("issued_at"), int) or not 0 <= time.time()-pending["issued_at"] <= 600:
            raise CounterpartyCreationError("Подтверждение истекло через 10 минут")
        name = self.clean_name(pending["name"])
        email = str(draft.sender_email or "").strip().casefold()
        if len(email) > 320 or len(emails(email)) != 1 or email not in emails(email):
            raise CounterpartyCreationError("Некорректный email; не создаю контрагента")
        key = f"oms:counterparty:customer:{draft.customer_id}"
        operation = await self.operations.store.get(key)
        if operation and operation["status"] != "succeeded":
            raise CounterpartyCreationError(
                "Создание могло пройти в МойСклад. Повторный POST запрещён до сверки.")
        payload = {"name": name, "email": email, "externalCode": key,
                   "description": f"OhMySmell · создан из оптового заказа по почте, CRM-клиент {draft.customer_id}. Без исходящих уведомлений."}
        if operation is None:
            duplicates = await self.matcher.duplicate_candidates(name=name, email=email)
            if duplicates:
                raise CounterpartyCreationError(
                    "Найден существующий контрагент по имени/email. Новый не создаю; выполните /cpsearch.")
        else:
            if operation["payload"] != payload:
                raise CounterpartyCreationError("Имя или email изменились после создания; нужна ручная сверка")
        # Journaling is serialized by Postgres advisory lock.
        async def send(document):
            return await self.matcher.async_gateway.create_counterparty(document)
        try:
            created = await self.operations.execute_counterparty(key, payload, send)
        except ExternalOperationConflict as error:
            raise CounterpartyCreationError(str(error)) from error
        identifier = created.get("id")
        if not identifier:
            raise CounterpartyCreationError("Ответ МойСклад без ID – требуется сверка")
        verified = await self.matcher.async_gateway.get_counterparty(identifier)
        if (verified.get("archived") or verified.get("id") != identifier
                or normalize_text(verified.get("name")) != normalize_text(name)
                or email not in emails(verified.get("email"))):
            raise CounterpartyCreationError("После создания реквизиты не совпадают; привязка заблокирована")
        try:
            return await self.repository.set_counterparty(
                draft_id, identifier, verified["name"], actor=manager_id,
                evidence="created_after_duplicate_check")
        except Exception:
            # The journal contains the exact provider id, so retry only the
            # CRM link after reconciliation, without another provider POST.
            raise
