import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.integrations.email.provider import EmailMessage
from app.models.draft_order import ProductMatchStatus
from app.models.sales import CustomerIdentityType, CustomerType
from app.services.customer_resolution_service import (
    CustomerResolution,
    CustomerResolutionService,
)
from app.services.draft_order_service import DraftOrderService
from app.services.email_parser import EmailParser
from app.services.order_lifecycle import OrderStatus
from app.services.price_service import PriceService
from app.services.product_matching_service import ProductMatchingService
from tests.test_sales_core import FakeCustomerRepository, existing_customer


PRODUCTS = [
    {
        "id": "chanel-1",
        "name": "Chanel Allure Homme Sport",
        "article": "CHA-001",
        "salePrices": [
            {"value": 15000, "priceType": {"name": "Цена продажи"}}
        ],
    },
    {
        "id": "marvis-85",
        "name": "Marvis Classic Strong Mint 85 ml",
        "article": "MARVIS-85",
        "salePrices": [
            {"value": 10000, "priceType": {"name": "Цена продажи"}}
        ],
    },
    {
        "id": "marvis-75",
        "name": "Marvis Classic Strong Mint 75 ml",
        "article": "MARVIS-75",
        "salePrices": [
            {"value": 9000, "priceType": {"name": "Цена продажи"}}
        ],
    },
]


class FakeCatalog:
    def get_products(self):
        return list(PRODUCTS)


class FakeCustomerService:
    def __init__(self, customer_type, counterparty_id=None, customer_id=1):
        self.resolution = CustomerResolution(
            customer_id, customer_type, counterparty_id
        )
        self.calls = 0

    async def resolve_email(self, email, display_name=None):
        self.calls += 1
        return self.resolution


class FakeCounterpartyService:
    def candidates(self, *queries):
        return []


class FakeDraftRepository:
    def __init__(self):
        self.draft = None
        self.customer_updated_to = None

    async def get_by_external_message_id(self, external_id):
        if self.draft and self.draft.external_message_id == external_id:
            return self.draft
        return None

    async def create(self, data):
        items = [
            SimpleNamespace(id=index, **item)
            for index, item in enumerate(data["items"], start=1)
        ]
        self.draft = SimpleNamespace(
            id=10,
            inbound_message_id=20,
            external_message_id=data["external_message_id"],
            customer_id=data["customer_id"],
            customer_type=data["customer_type"],
            source="email",
            status=data["status"],
            sender_email=data["sender_email"],
            customer_name=data["customer_name"],
            subject=data["subject"],
            counterparty_id=data["counterparty_id"],
            counterparty_name=None,
            counterparty_candidates=data["counterparty_candidates"],
            total=data["total"],
            review_notes="\n".join(data["problems"]) or None,
            finalized_order_id=None,
            created_at=datetime.now(timezone.utc),
            items=items,
        )
        return self.draft

    async def get(self, draft_id):
        return self.draft if self.draft and self.draft.id == draft_id else None

    async def set_customer_type(self, draft_id, customer_type):
        self.customer_updated_to = customer_type
        self.draft.customer_type = customer_type
        return self.draft

    async def save_review(
        self, draft_id, status, total, problems, priced_items
    ):
        self.draft.status = status
        self.draft.total = total
        self.draft.review_notes = "\n".join(problems) or None
        for item in self.draft.items:
            pricing = priced_items.get(item.id)
            item.price = pricing[0] if pricing else None
            item.item_total = pricing[1] if pricing else None
        return self.draft


def email_message(external_id="email-1", body="Chanel Allure Homme Sport x1"):
    return EmailMessage(
        external_message_id=external_id,
        sender_email="buyer@example.com",
        sender_name="Buyer",
        subject="Order",
        body_text=body,
        received_at=datetime.now(timezone.utc),
    )


def draft_service(customer_type, counterparty_id=None, notifier=None):
    repository = FakeDraftRepository()
    customer_service = FakeCustomerService(customer_type, counterparty_id)
    service = DraftOrderService(
        repository=repository,
        customer_service=customer_service,
        matching_service=ProductMatchingService(FakeCatalog()),
        price_service=PriceService(
            {CustomerType.WHOLESALE: "Цена продажи", CustomerType.RETAIL: ""}
        ),
        counterparty_service=FakeCounterpartyService(),
        notifier=notifier or AsyncMock(),
    )
    return service, repository, customer_service


class TestEmailParser:
    def test_extracts_supported_quantities(self):
        parsed = EmailParser().parse_lines(
            "Chanel Allure Homme Sport 2 шт\n"
            "Marvis Classic Strong Mint 85 ml - 3\n"
            "Rituals Dream Collection Body Mist x1"
        )
        assert [(line.raw_product_text, line.qty) for line in parsed] == [
            ("Chanel Allure Homme Sport", 2),
            ("Marvis Classic Strong Mint 85 ml", 3),
            ("Rituals Dream Collection Body Mist", 1),
        ]


class TestProductMatching:
    def test_exact_article_and_exact_name(self):
        service = ProductMatchingService(FakeCatalog())
        assert service.match("CHA-001", 1).status == ProductMatchStatus.MATCHED
        assert (
            service.match("Chanel Allure Homme Sport", 1).status
            == ProductMatchStatus.MATCHED
        )

    def test_ambiguous_match_returns_candidates(self):
        result = ProductMatchingService(FakeCatalog()).match(
            "Marvis Classic Strong Mint", 1
        )
        assert result.status == ProductMatchStatus.AMBIGUOUS
        assert len(result.candidates) == 2

    def test_not_found(self):
        result = ProductMatchingService(FakeCatalog()).match(
            "Completely unknown product qwerty", 1
        )
        assert result.status == ProductMatchStatus.NOT_FOUND


class TestEmailCustomerResolution:
    def test_existing_wholesale_customer_stays_wholesale(self):
        customer = existing_customer(
            CustomerType.WHOLESALE,
            (CustomerIdentityType.EMAIL, "buyer@example.com"),
        )
        result = asyncio.run(
            CustomerResolutionService(
                FakeCustomerRepository([customer])
            ).resolve_email("BUYER@example.com")
        )
        assert result.customer_type == CustomerType.WHOLESALE

    def test_existing_retail_customer_does_not_become_wholesale(self):
        customer = existing_customer(
            CustomerType.RETAIL,
            (CustomerIdentityType.EMAIL, "buyer@example.com"),
        )
        result = asyncio.run(
            CustomerResolutionService(
                FakeCustomerRepository([customer])
            ).resolve_email("buyer@example.com")
        )
        assert result.customer_type == CustomerType.RETAIL

    def test_new_email_customer_is_unknown(self):
        result = asyncio.run(
            CustomerResolutionService(FakeCustomerRepository()).resolve_email(
                "new@example.com"
            )
        )
        assert result.customer_type == CustomerType.UNKNOWN


class TestDraftOrderPipeline:
    def test_unknown_customer_needs_review(self):
        service, _, _ = draft_service(CustomerType.UNKNOWN)
        draft = asyncio.run(service.ingest_email(email_message()))
        assert draft.status == OrderStatus.NEEDS_REVIEW
        assert "тип клиента" in draft.review_notes

    def test_ambiguous_item_needs_review(self):
        service, _, _ = draft_service(
            CustomerType.WHOLESALE, "counterparty-1"
        )
        draft = asyncio.run(
            service.ingest_email(
                email_message(body="Marvis Classic Strong Mint x1")
            )
        )
        assert draft.status == OrderStatus.NEEDS_REVIEW
        assert draft.items[0].match_status == ProductMatchStatus.AMBIGUOUS

    def test_fully_resolved_draft_is_ready(self):
        service, _, _ = draft_service(
            CustomerType.WHOLESALE, "counterparty-1"
        )
        draft = asyncio.run(service.ingest_email(email_message()))
        assert draft.status == OrderStatus.READY
        assert draft.total == 15000

    def test_manager_confirmation_wholesale_updates_customer(self):
        service, repository, _ = draft_service(
            CustomerType.UNKNOWN, "counterparty-1"
        )
        draft = asyncio.run(service.ingest_email(email_message()))
        reviewed = asyncio.run(
            service.set_customer_type(draft.id, CustomerType.WHOLESALE)
        )
        assert repository.customer_updated_to == CustomerType.WHOLESALE
        assert reviewed.customer_type == CustomerType.WHOLESALE
        assert reviewed.status == OrderStatus.READY

    def test_duplicate_external_id_is_idempotent(self):
        notifier = AsyncMock()
        service, repository, customer_service = draft_service(
            CustomerType.WHOLESALE, "counterparty-1", notifier
        )
        first = asyncio.run(service.ingest_email(email_message()))
        second = asyncio.run(service.ingest_email(email_message()))
        assert first is second
        assert repository.draft is first
        assert customer_service.calls == 1
        notifier.assert_awaited_once()

    def test_telegram_failure_does_not_break_ingestion(self):
        notifier = AsyncMock(side_effect=RuntimeError("Telegram unavailable"))
        service, _, _ = draft_service(
            CustomerType.WHOLESALE, "counterparty-1", notifier
        )
        draft = asyncio.run(service.ingest_email(email_message()))
        assert draft.id == 10
        assert draft.status == OrderStatus.READY
