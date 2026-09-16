import logging

from app.integrations.email.provider import EmailMessage
from app.logging_utils import log_event
from app.models.draft_order import DraftOrder, ProductMatchStatus
from app.models.sales import CustomerType
from app.repositories.draft_order_repository import (
    DraftOrderRepository,
    DuplicateInboundMessageError,
)
from app.services.counterparty_matching_service import CounterpartyMatchingService
from app.services.customer_resolution_service import CustomerResolutionService
from app.services.draft_telegram_service import notify_managers_about_draft
from app.services.order_extraction_service import OrderExtractionService
from app.services.order_lifecycle import (
    InvalidOrderTransitionError,
    OrderStatus,
)
from app.services.price_service import (
    PriceConfigurationError,
    PriceNotConfiguredError,
    PriceService,
)
from app.services.product_matching_service import ProductMatchingService


logger = logging.getLogger(__name__)


class DraftOrderError(Exception):
    pass


def calculate_draft_total(
    items,
    priced_items: dict[int, tuple[int, int]] | None = None,
) -> int | None:
    """Return an exact minor-unit total only for fully resolved/priced items."""
    items = list(items)
    if not items:
        return None
    total = 0
    for item in items:
        value = item.get if isinstance(item, dict) else lambda key: getattr(item, key)
        if (
            ProductMatchStatus(value("match_status")) != ProductMatchStatus.MATCHED
            or not value("product_id")
        ):
            return None
        if priced_items is None:
            price = value("price")
            item_total = value("item_total")
        else:
            pricing = priced_items.get(value("id"))
            if pricing is None:
                return None
            price, item_total = pricing
        if price is None or item_total is None:
            return None
        if type(price) is not int or price < 0 or type(item_total) is not int or item_total < 0:
            return None
        total += item_total
        if total > 9_223_372_036_854_775_807:
            return None
    return total


class DraftOrderService:
    def __init__(
        self,
        repository=None,
        customer_service=None,
        extraction_service=None,
        matching_service=None,
        price_service=None,
        counterparty_service=None,
        notifier=None,
    ) -> None:
        self.repository = repository or DraftOrderRepository()
        self.customer_service = customer_service or CustomerResolutionService()
        self.extraction_service = extraction_service or OrderExtractionService()
        self.matching_service = matching_service or ProductMatchingService()
        self.price_service = price_service or PriceService()
        self.counterparty_service = (
            counterparty_service or CounterpartyMatchingService()
        )
        self.notifier = notifier or notify_managers_about_draft

    async def ingest_email(self, message: EmailMessage) -> DraftOrder:
        existing = await self.repository.get_by_external_message_id(
            message.external_message_id
        )
        if existing is not None:
            log_event(
                logger,
                "duplicate_email_skipped",
                action="email_duplicate",
                draft_id=existing.id,
            )
            return existing

        customer = await self.customer_service.resolve_email(
            message.sender_email,
            message.sender_name,
        )
        try:
            return await self._ingest_resolved_email(message, customer)
        except Exception:
            try:
                await self.customer_service.discard_if_unreferenced(customer)
            except Exception:
                logger.error(
                    "Failed to discard unreferenced customer after ingestion error"
                )
            raise

    async def _ingest_resolved_email(
        self,
        message: EmailMessage,
        customer,
        source="email",
    ) -> DraftOrder:
        log_event(
            logger,
            "customer_resolved",
            customer_id=customer.customer_id,
            customer_type=customer.customer_type.value,
        )
        extracted_lines = self.extraction_service.extract(message.body_text)
        log_event(
            logger,
            "email_parsed",
            action="email_parse",
            extracted_count=len(extracted_lines),
        )
        matches = [
            await self.matching_service.match_async(line.raw_product_text, line.qty)
            for line in extracted_lines
        ]
        log_event(
            logger,
            "products_matched",
            matched=sum(m.status == ProductMatchStatus.MATCHED for m in matches),
            ambiguous=sum(m.status == ProductMatchStatus.AMBIGUOUS for m in matches),
            not_found=sum(m.status == ProductMatchStatus.NOT_FOUND for m in matches),
        )

        counterparty_candidates = []
        if not customer.moysklad_counterparty_id:
            try:
                counterparty_candidates = await self.counterparty_service.candidates_async(
                    message.sender_email,
                    message.sender_name,
                    *customer.identities.get("phone", []),
                )
            except Exception:
                logger.warning("Counterparty lookup failed for inbound email")

        items = []
        problems = []
        if not extracted_lines:
            problems.append("Не удалось извлечь позиции из письма")
        if customer.customer_type == CustomerType.UNKNOWN:
            problems.append("Необходимо подтвердить тип клиента")
        if not customer.moysklad_counterparty_id:
            problems.append("Необходимо выбрать контрагента")

        for match in matches:
            item = {
                "raw_product_text": match.raw_product_text,
                "qty": match.qty,
                "match_status": match.status,
                "product_id": None,
                "product_name": None,
                "article": None,
                "price": None,
                "item_total": None,
                "candidates": [candidate.__dict__ for candidate in match.candidates],
            }
            if match.status != ProductMatchStatus.MATCHED or match.product is None:
                problems.append(
                    f"{match.raw_product_text}: {match.status.value}"
                )
            else:
                item.update(
                    product_id=match.product["id"],
                    product_name=match.product.get("name"),
                    article=match.product.get("article"),
                )
                price = self._price(match.product, customer.customer_type, problems)
                if price is not None:
                    if price * match.qty > 9_223_372_036_854_775_807:
                        problems.append("Сумма позиции превышает допустимый предел")
                    else:
                        item["price"] = price
                        item["item_total"] = price * match.qty
            items.append(item)

        if items and calculate_draft_total(items) is None and not problems:
            problems.append("Итоговая сумма требует проверки")
        status = OrderStatus.NEEDS_REVIEW if problems else OrderStatus.READY
        data = {
            "source": source,
            "external_message_id": message.external_message_id,
            "sender_email": message.sender_email,
            "customer_name": message.sender_name,
            "subject": message.subject,
            "body_text": message.body_text,
            "received_at": message.received_at,
            "customer_id": customer.customer_id,
            "customer_type": customer.customer_type,
            "counterparty_id": customer.moysklad_counterparty_id,
            "counterparty_candidates": counterparty_candidates,
            "status": status,
            "total": calculate_draft_total(items),
            "problems": problems,
            "items": items,
        }
        try:
            draft = await self.repository.create(data)
        except DuplicateInboundMessageError:
            draft = await self.repository.get_by_external_message_id(
                message.external_message_id
            )
            if draft is None:
                raise
            return draft

        log_event(
            logger,
            "draft_created",
            draft_id=draft.id,
            status=draft.status.value if hasattr(draft.status, "value") else draft.status,
        )

        try:
            await self.notifier(draft)
            if hasattr(self.repository, "mark_notified"):
                await self.repository.mark_notified(draft.id)
        except Exception:
            logger.warning("Draft %s saved, Telegram notification pending retry", draft.id)
        return draft

    async def ingest_channel(self, envelope):
        """Authenticated server adapters only; shared extraction/review core."""
        from datetime import datetime, timezone
        from app.schemas.order import OrderCreate
        from app.models.sales import CustomerIdentityType
        identity_fields = {"email": "email", "phone": "phone", "telegram": "telegram",
                           "instagram": "instagram_username"}
        kind = CustomerIdentityType(envelope.identity_type).value
        if not envelope.identity_value.strip() or not envelope.external_id:
            raise DraftOrderError("Отсутствует идентификатор сообщения или клиента")
        external_id = f"{envelope.source}:{kind}:{envelope.external_id}"
        if len(external_id) > 512:
            raise DraftOrderError("Идентификатор сообщения слишком длинный")
        existing = await self.repository.get_by_external_message_id(external_id)
        if existing:
            return existing
        data = {"customer_name": envelope.display_name or envelope.identity_value,
                "phone": "", "source": envelope.source,
                identity_fields[kind]: envelope.identity_value,
                "items": [{"id": "unresolved", "qty": 1}]}
        customer = await self.customer_service.resolve(OrderCreate(**data))
        message = EmailMessage(external_id, envelope.identity_value, envelope.display_name,
                               "Входящая заявка", envelope.text, datetime.now(timezone.utc))
        try:
            return await self._ingest_resolved_email(message, customer, source=envelope.source)
        except Exception:
            await self.customer_service.discard_if_unreferenced(customer)
            raise

    def _price(
        self, product: dict, customer_type: CustomerType, problems: list[str]
    ) -> int | None:
        try:
            price = self.price_service.get_price(
                product, customer_type
            ).amount_minor
        except (PriceConfigurationError, PriceNotConfiguredError) as error:
            problems.append(str(error))
            return None
        return price

    async def review(self, draft_id: int, draft=None) -> DraftOrder:
        draft = draft or await self._get_required(draft_id)
        if getattr(draft, "finalized_order_id", None):
            return draft
        self._ensure_reviewable(draft)
        problems = []
        priced_items = {}
        products = {
            item["id"]: item
            for item in await self.matching_service.products_async()
        }

        if CustomerType(draft.customer_type) == CustomerType.UNKNOWN:
            problems.append("Необходимо подтвердить тип клиента")
        if not draft.counterparty_id:
            problems.append("Необходимо выбрать контрагента")
        if not draft.items:
            problems.append("В draft отсутствуют позиции")

        for item in draft.items:
            if item.match_status != ProductMatchStatus.MATCHED or not item.product_id:
                problems.append(f"{item.raw_product_text}: {item.match_status}")
                continue
            product = products.get(item.product_id)
            if product is None:
                problems.append(f"Товар {item.product_id} больше не найден в каталоге")
                continue
            price = self._price(product, CustomerType(draft.customer_type), problems)
            if price is not None:
                if price * item.qty > 9_223_372_036_854_775_807:
                    problems.append("Сумма позиции превышает допустимый предел")
                else:
                    priced_items[item.id] = (price, price * item.qty)

        total = calculate_draft_total(draft.items, priced_items)
        if total is None and not problems:
            problems.append("Итоговая сумма требует проверки")

        target = OrderStatus.NEEDS_REVIEW if problems else OrderStatus.READY
        try:
            reviewed = await self.repository.save_review(
                draft_id,
                target,
                total,
                problems,
                priced_items,
                **({"expected_revision": draft.revision} if hasattr(draft, "revision") else {}),
            )
        except InvalidOrderTransitionError as error:
            raise DraftOrderError(str(error)) from error
        if reviewed is None:
            raise DraftOrderError("Draft не найден")
        log_event(logger, "draft_reviewed", draft_id=draft_id, action="review", result=str(reviewed.status))
        return reviewed

    async def set_customer_type(
        self, draft_id: int, customer_type: CustomerType
    ) -> DraftOrder:
        draft = await self.repository.set_customer_type(draft_id, customer_type)
        if draft is None:
            raise DraftOrderError("Draft не найден")
        return await self.review(draft_id, draft)

    async def resolve_product(
        self, draft_id: int, item_id: int, product_id: str
    ) -> DraftOrder:
        product = next(
            (
                item
                for item in await self.matching_service.products_async()
                if item["id"] == product_id
            ),
            None,
        )
        if product is None:
            raise DraftOrderError("Товар МойСклад не найден")
        draft = await self.repository.resolve_item(draft_id, item_id, product)
        if draft is None:
            raise DraftOrderError("Draft или позиция не найдены")
        return await self.review(draft_id, draft)

    async def link_counterparty(
        self, draft_id: int, counterparty_id: str, counterparty_name: str
    ) -> DraftOrder:
        draft = await self.repository.set_counterparty(
            draft_id, counterparty_id, counterparty_name
        )
        if draft is None:
            raise DraftOrderError("Draft не найден")
        return await self.review(draft_id, draft)

    async def load_counterparty_candidates(
        self, draft_id: int
    ) -> DraftOrder:
        draft = await self._get_required(draft_id)
        self._ensure_reviewable(draft)
        candidates = await self.counterparty_service.fallback_candidates_async(
            draft.sender_email,
            draft.customer_name,
        )
        updated = await self.repository.set_counterparty_candidates(
            draft_id, candidates
        )
        if updated is None:
            raise DraftOrderError("Draft not found")
        return updated

    async def search_item(self, draft_id, item_id, query):
        match = await self.matching_service.match_async(query[:500], 1)
        candidates = [candidate.__dict__ for candidate in match.candidates]
        if match.product:
            candidates = [{"id": match.product["id"], "name": match.product.get("name"),
                           "article": match.product.get("article"), "score": 1.0}]
        draft = await self.repository.set_item_candidates(draft_id, item_id, candidates)
        if draft is None:
            raise DraftOrderError("Черновик не найден")
        return draft

    async def reject(self, draft_id: int) -> DraftOrder:
        try:
            draft = await self.repository.reject(draft_id)
        except InvalidOrderTransitionError as error:
            raise DraftOrderError(str(error)) from error
        if draft is None:
            raise DraftOrderError("Draft не найден")
        return draft

    async def finalize(self, draft_id: int):
        existing = await self._get_required(draft_id)
        if existing.finalized_order_id:
            order = await self.repository.finalize(draft_id)
            if order is None:
                raise DraftOrderError("Связанный заказ не найден")
            return order
        draft = await self.review(draft_id, existing)
        if getattr(draft, "finalized_order_id", None):
            return await self.repository.finalize(draft_id)
        problems = self._finalize_problems(draft)
        if problems:
            raise DraftOrderError(
                "Draft cannot be finalized: " + "; ".join(problems)
            )
        try:
            order = await self.repository.finalize(draft_id)
        except InvalidOrderTransitionError as error:
            raise DraftOrderError(str(error)) from error
        if order is None:
            raise DraftOrderError("Draft не найден")
        log_event(logger, "draft_finalized", draft_id=draft_id, order_id=order.id)
        return order

    @staticmethod
    def _finalize_problems(draft: DraftOrder) -> list[str]:
        problems = []
        if CustomerType(draft.customer_type) == CustomerType.UNKNOWN:
            problems.append("customer type is unknown")
        if not draft.counterparty_id:
            problems.append("counterparty is not selected")
        if draft.total is None:
            problems.append("draft total is missing")
        if not draft.items:
            problems.append("order has no items")
        for item in draft.items:
            if (
                item.match_status != ProductMatchStatus.MATCHED
                or not item.product_id
            ):
                problems.append(f"unresolved product: {item.raw_product_text}")
            elif item.price is None or item.item_total is None:
                problems.append(f"price is missing: {item.raw_product_text}")
        if draft.status != OrderStatus.READY:
            problems.append("draft is not ready")
        return problems

    async def _get_required(self, draft_id: int) -> DraftOrder:
        draft = await self.repository.get(draft_id)
        if draft is None:
            raise DraftOrderError("Draft не найден")
        expected = getattr(self.repository, "expected_revision", None)
        if expected is not None and not draft.finalized_order_id and draft.revision != expected:
            raise DraftOrderError("Карточка устарела; обновите черновик")
        return draft

    @staticmethod
    def _ensure_reviewable(draft: DraftOrder) -> None:
        if OrderStatus(draft.status) not in {
            OrderStatus.DRAFT,
            OrderStatus.NEEDS_REVIEW,
            OrderStatus.READY,
        }:
            raise DraftOrderError(
                f"Draft в статусе {draft.status} больше нельзя редактировать"
            )
