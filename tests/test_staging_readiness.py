import asyncio
import base64
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect

from app.api.auth import require_debug_access, require_internal_api_token
from app.config.settings import settings
from app.database.base import Base
from app.integrations.email.gmail_provider import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SCOPES,
    GmailEmailProvider,
)
from app.integrations.email.provider import EmailFetchBatch, EmailMessage
from app.integrations.moysklad.client import MoySkladClient
from app.models.sales import CustomerType
from app.services.money import format_rubles, minor_to_major
from app.services.order_validation_service import OrderValidationService
from app.services.price_service import PriceService
from app.workers.email_ingestion import EmailIngestionWorker


def encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


class TestMoney:
    def test_99_99_is_exact_minor_units(self):
        product = {
            "name": "Exact price",
            "salePrices": [
                {"value": 9999, "priceType": {"name": "Цена продажи"}}
            ],
        }
        price = PriceService(
            {CustomerType.WHOLESALE: "Цена продажи"}
        ).get_price(product, CustomerType.WHOLESALE)
        assert price.amount_minor == 9999
        assert minor_to_major(price.amount_minor).as_tuple().exponent == -2
        assert format_rubles(price.amount_minor) == "99.99"

    def test_quantity_total_has_no_float_error(self):
        product_service = SimpleNamespace(
            get_catalog=lambda **kwargs: [
                {
                    "id": "p1",
                    "name": "Product",
                    "article": None,
                    "price": 9999,
                    "total_available": 10,
                }
            ]
        )
        order = SimpleNamespace(
            customer_type=CustomerType.WHOLESALE,
            customer_name="Buyer",
            phone="",
            source="email",
            telegram=None,
            comment=None,
            items=[SimpleNamespace(id="p1", qty=3)],
        )
        result = OrderValidationService(product_service).validate(order)
        assert result["items"][0]["sum"] == 29997
        assert result["total"] == 29997

    def test_moysklad_internal_round_trip_keeps_minor_units(self):
        client = MoySkladClient.__new__(MoySkladClient)
        client.base_url = "https://example.invalid/api"
        client.headers = {}
        client.session = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"id": "order-1"}
        with patch.object(client.session, "post", return_value=response) as post:
            client.create_customer_order(
                "org", "counterparty", [{"id": "p1", "qty": 2, "price": 9999}]
            )
        assert post.call_args.kwargs["json"]["positions"][0]["price"] == 9999


class TestGmailAdapter:
    def test_plain_text_body(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": encoded("Order x2")}},
                {"mimeType": "text/html", "body": {"data": encoded("<b>ignored</b>")}},
            ],
        }
        assert GmailEmailProvider._body_text(payload) == "Order x2"

    def test_html_fallback_is_safe_text(self):
        payload = {
            "mimeType": "text/html",
            "body": {
                "data": encoded(
                    "<p>Product x1</p><script>secret()</script><div>Next</div>"
                )
            },
        }
        text = GmailEmailProvider._body_text(payload)
        assert "Product x1" in text
        assert "Next" in text
        assert "secret" not in text

    def test_only_readonly_scope_is_declared(self):
        assert GMAIL_SCOPES == (GMAIL_READONLY_SCOPE,)
        assert all("modify" not in scope and "send" not in scope for scope in GMAIL_SCOPES)

    def test_message_metadata_and_plain_body_are_parsed(self):
        raw = {
            "id": "gmail-1",
            "labelIds": ["INBOX", "UNREAD"],
            "internalDate": "1704067200000",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "Buyer <buyer@example.com>"},
                    {"name": "Subject", "value": "Order"},
                ],
                "body": {"data": encoded("Product x2")},
            },
        }
        service = Mock()
        service.users.return_value.messages.return_value.get.return_value.execute.return_value = raw
        message = GmailEmailProvider(service)._get_message(service, "gmail-1")
        assert message.external_message_id == "gmail-1"
        assert message.sender_email == "buyer@example.com"
        assert message.sender_name == "Buyer"
        assert message.subject == "Order"
        assert message.body_text == "Product x2"
        assert message.received_at == datetime(2024, 1, 1, tzinfo=timezone.utc)


class MemoryCursorRepository:
    def __init__(self):
        self.value = None

    async def get(self, provider):
        return self.value

    async def set(self, provider, value):
        self.value = value


class RepeatingProvider:
    def __init__(self, messages):
        self.messages = messages
        self.seen_cursors = []

    async def fetch_unprocessed(self, cursor=None):
        self.seen_cursors.append(cursor)
        return EmailFetchBatch(self.messages, "history-2")


class IdempotentPipeline:
    def __init__(self, malformed_id=None):
        self.drafts = {}
        self.created = 0
        self.calls = []
        self.malformed_id = malformed_id

    async def ingest_email(self, message):
        self.calls.append(message.external_message_id)
        if message.external_message_id == self.malformed_id:
            raise ValueError("malformed")
        if message.external_message_id not in self.drafts:
            self.created += 1
            self.drafts[message.external_message_id] = SimpleNamespace(id=self.created)
        return self.drafts[message.external_message_id]


def worker_message(message_id):
    return EmailMessage(
        message_id,
        "sender@example.com",
        None,
        "Order",
        "Product x1",
        datetime.now(timezone.utc),
    )


class TestEmailWorker:
    def test_duplicate_after_restart_does_not_create_second_draft(self):
        provider = RepeatingProvider([worker_message("m1")])
        cursor = MemoryCursorRepository()
        pipeline = IdempotentPipeline()
        asyncio.run(EmailIngestionWorker(provider, pipeline, cursor).run_once())
        asyncio.run(EmailIngestionWorker(provider, pipeline, cursor).run_once())
        assert pipeline.created == 1
        assert provider.seen_cursors == [None, "history-2"]

    def test_worker_continues_after_malformed_message(self):
        provider = RepeatingProvider([worker_message("bad"), worker_message("good")])
        pipeline = IdempotentPipeline(malformed_id="bad")
        stats = asyncio.run(
            EmailIngestionWorker(
                provider, pipeline, MemoryCursorRepository()
            ).run_once()
        )
        assert stats == {"received": 2, "processed": 1, "failed": 1}
        assert pipeline.calls == ["bad", "good"]


class TestInternalAuth:
    def test_missing_token_rejected_and_correct_token_allowed(self):
        old = settings.internal_api_token
        settings.internal_api_token = "staging-secret"
        try:
            with pytest.raises(HTTPException) as error:
                asyncio.run(require_internal_api_token(None))
            assert error.value.status_code == 401
            asyncio.run(require_internal_api_token("staging-secret"))
        finally:
            settings.internal_api_token = old

    def test_debug_disabled_returns_404(self):
        old_enabled = settings.debug_endpoints_enabled
        settings.debug_endpoints_enabled = False
        try:
            with pytest.raises(HTTPException) as error:
                asyncio.run(require_debug_access("anything"))
            assert error.value.status_code == 404
        finally:
            settings.debug_endpoints_enabled = old_enabled

    def test_internal_and_debug_routes_are_wired_to_auth_dependencies(self):
        from app.main import app

        routes = {
            (route.path, method): {
                dependency.call for dependency in route.dependant.dependencies
            }
            for route in app.routes
            if hasattr(route, "dependant")
            for method in route.methods
        }
        assert require_internal_api_token in routes[("/orders", "GET")]
        assert require_internal_api_token in routes[("/draft-orders", "GET")]
        assert require_internal_api_token in routes[("/internal/email/messages", "POST")]
        assert require_debug_access in routes[("/debug-products", "GET")]


class TestTelegramCallbacks:
    def test_draft_callback_router_filter_still_matches_callback_data(self):
        from app.bot import telegram_bot

        handler = next(
            item
            for item in telegram_bot.dp.callback_query.handlers
            if item.callback is telegram_bot.draft_callback_handler
        )
        matched, _ = asyncio.run(handler.check(SimpleNamespace(
            data="draft:type:wholesale:2"
        )))
        not_matched, _ = asyncio.run(handler.check(SimpleNamespace(
            data="other:type:wholesale:2"
        )))
        assert matched is True
        assert not_matched is False

    def test_reject_callback_delegates_to_service(self):
        from app.bot import telegram_bot

        service = SimpleNamespace(reject=AsyncMock(return_value=SimpleNamespace(
            id=5,
            status="rejected",
            customer_name=None,
            sender_email="x@example.com",
            customer_type="unknown",
            subject=None,
            counterparty_name=None,
            counterparty_id=None,
            items=[],
            total=None,
            review_notes=None,
            counterparty_candidates=[],
        )))
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            data="draft:reject:5",
            answer=AsyncMock(),
            message=None,
        )
        with (
            patch.object(telegram_bot, "is_active_manager", return_value=True),
            patch.object(telegram_bot, "DraftOrderService", return_value=service),
        ):
            asyncio.run(telegram_bot.draft_callback_handler(callback))
        service.reject.assert_awaited_once_with(5)


class TestFreshSchema:
    def test_current_metadata_contains_complete_fresh_schema(self):
        import app.models  # noqa: F401

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        tables = set(inspect(engine).get_table_names())
        assert {
            "managers",
            "orders",
            "order_items",
            "customers",
            "customer_identities",
            "inbound_messages",
            "draft_orders",
            "draft_order_items",
            "email_provider_cursors",
        }.issubset(tables)

    def test_alembic_graph_has_one_head_and_declares_all_tables(self):
        from pathlib import Path

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        scripts = ScriptDirectory.from_config(Config("alembic.ini"))
        assert scripts.get_heads() == ["f59b1c43de76"]
        sales_revision = scripts.get_revision("a84f1b92c301")
        assert sales_revision.dependencies == "f01a2b3c4d5e"

        migration_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in Path("alembic/versions").glob("*.py")
        )
        for table in Base.metadata.tables:
            assert f'"{table}"' in migration_text or f"'{table}'" in migration_text
