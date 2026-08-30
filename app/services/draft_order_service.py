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
                message_id=message.external_message_id,
                draft_id=existing.id,
            )
            return existing

        customer = await self.customer_service.resolve_email(
            message.sender_email,
            message.sender_name,
        )
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
            message_id=message.external_message_id,
            extracted_count=len(extracted_lines),
        )
        matches = [
            self.matching_service.match(line.raw_product_text, line.qty)
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
                counterparty_candidates = self.counterparty_service.candidates(
                    message.sender_email,
                    message.sender_name,
                    *customer.identities.get("phone", []),
                )
            except Exception:
                logger.exception("Counterparty lookup failed for inbound email")

        items = []
        problems = []
        total = 0
        all_priced = True
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
                all_priced = False
            else:
                item.update(
                    product_id=match.product["id"],
                    product_name=match.product.get("name"),
                    article=match.product.get("article"),
                )
                price = self._price(match.product, customer.customer_type, problems)
                if price is None:
                    all_priced = False
                else:
                    item["price"] = price
                    item["item_total"] = price * match.qty
                    total += item["item_total"]
            items.append(item)

        status = OrderStatus.NEEDS_REVIEW if problems else OrderStatus.READY
        data = {
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
            "total": total if all_priced and items else None,
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
        except Exception:
            logger.exception("Draft %s saved, Telegram notification failed", draft.id)
        return draft

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

    async def review(self, draft_id: int) -> DraftOrder:
        draft = await self._get_required(draft_id)
        problems = []
        priced_items = {}
        total = 0
        products = {item["id"]: item for item in self.matching_service.products()}

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
                priced_items[item.id] = (price, price * item.qty)
                total += price * item.qty

        target = OrderStatus.NEEDS_REVIEW if problems else OrderStatus.READY
        try:
            reviewed = await self.repository.save_review(
                draft_id,
                target,
                None if problems else total,
                problems,
                priced_items,
            )
        except InvalidOrderTransitionError as error:
            raise DraftOrderError(str(error)) from error
        if reviewed is None:
            raise DraftOrderError("Draft не найден")
        return reviewed

    async def set_customer_type(
        self, draft_id: int, customer_type: CustomerType
    ) -> DraftOrder:
        self._ensure_reviewable(await self._get_required(draft_id))
        draft = await self.repository.set_customer_type(draft_id, customer_type)
        if draft is None:
            raise DraftOrderError("Draft не найден")
        return await self.review(draft_id)

    async def resolve_product(
        self, draft_id: int, item_id: int, product_id: str
    ) -> DraftOrder:
        self._ensure_reviewable(await self._get_required(draft_id))
        product = next(
            (item for item in self.matching_service.products() if item["id"] == product_id),
            None,
        )
        if product is None:
            raise DraftOrderError("Товар МойСклад не найден")
        draft = await self.repository.resolve_item(draft_id, item_id, product)
        if draft is None:
            raise DraftOrderError("Draft или позиция не найдены")
        return await self.review(draft_id)

    async def link_counterparty(
        self, draft_id: int, counterparty_id: str, counterparty_name: str
    ) -> DraftOrder:
        self._ensure_reviewable(await self._get_required(draft_id))
        draft = await self.repository.set_counterparty(
            draft_id, counterparty_id, counterparty_name
        )
        if draft is None:
            raise DraftOrderError("Draft не найден")
        return await self.review(draft_id)

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
        draft = await self.review(draft_id)
        if draft.status != OrderStatus.READY:
            raise DraftOrderError("Draft требует проверки и не может быть финализирован")
        try:
            order = await self.repository.finalize(draft_id)
        except InvalidOrderTransitionError as error:
            raise DraftOrderError(str(error)) from error
        if order is None:
            raise DraftOrderError("Draft не найден")
        log_event(logger, "draft_finalized", draft_id=draft_id, order_id=order.id)
        return order

    async def _get_required(self, draft_id: int) -> DraftOrder:
        draft = await self.repository.get(draft_id)
        if draft is None:
            raise DraftOrderError("Draft не найден")
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
