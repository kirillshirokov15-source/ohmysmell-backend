import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from pydantic import ValidationError
from app.config.settings import settings
from app.integrations.write_guard import ExternalWritesDisabled
from app.integrations.moysklad.client import MoySkladClient
from app.integrations.moysklad.payloads import customerorder, demand
from app.services.price_service import PriceService, PriceConfigurationError, PriceNotConfiguredError
from app.models.sales import CustomerType
from app.services.product_matching_service import ProductMatchingService
from app.services.stock_allocation import allocate, available_units, StockAllocationError
from app.schemas.checkout import CheckoutCreate
from app.services.checkout_service import request_hash
from tests.asgi_client import request


def checkout_payload():
    return {"customer_name": "Test", "email": "test@example.invalid", "phone": "+79990000000",
            "items": [{"product_id": "p1", "qty": 2}]}


@pytest.mark.parametrize("qty", [True, False, 1.5, "2", 0, -1, 10001])
def test_checkout_rejects_invalid_quantity(qty):
    data = checkout_payload()
    data["items"][0]["qty"] = qty
    with pytest.raises(ValidationError):
        CheckoutCreate(**data)


@pytest.mark.parametrize("field,value", [("price", 1), ("customer_type", "wholesale"), ("source", "manual"), ("customer_id", 1)])
def test_public_request_cannot_supply_authority(field, value):
    with pytest.raises(ValidationError):
        CheckoutCreate(**{**checkout_payload(), field: value})


@pytest.mark.parametrize("value", [-1, True, 1.5, float("nan"), 2**63])
def test_invalid_money_is_controlled(value):
    with pytest.raises(PriceNotConfiguredError):
        PriceService({CustomerType.WHOLESALE: "W"}).get_price(
            {"salePrices": [{"priceType": {"name": "W"}, "value": value}]}, CustomerType.WHOLESALE)


def test_retail_misconfiguration_cannot_alias_wholesale():
    with pytest.raises(PriceConfigurationError):
        PriceService({CustomerType.WHOLESALE: "W", CustomerType.RETAIL: "W"}).get_price({}, CustomerType.RETAIL)


@pytest.mark.parametrize("query,expected", [("", "not_found"), ("!!!", "not_found"), ("code-1", "matched"), ("Primary", "matched"), ("Primar", "ambiguous")])
def test_matching_code_primary_empty_and_fuzzy(query, expected):
    product = {"id": "p1", "name": "Primary/Русское название", "code": "code-1"}
    service = ProductMatchingService(SimpleNamespace(get_products=lambda: [product, product]))
    assert service.match(query, 1).status == expected


def test_duplicate_codes_do_not_silently_match():
    service = ProductMatchingService(SimpleNamespace(get_products=lambda: [
        {"id": "p1", "code": "a"}, {"id": "p2", "code": "a"}]))
    result = service.match("a", 1)
    assert result.status == "ambiguous"
    assert len(result.candidates) == 2


@pytest.mark.parametrize("stock,reserve,expected", [(10, 4, 6), (1, 3, 0), ("3.9", "1.1", 2), (-1, 0, 0)])
def test_stock_available(stock, reserve, expected):
    assert available_units(stock, reserve) == expected


def test_two_warehouse_split_and_duplicate_lines():
    catalog = [{"id": "p1", "stocks": [{"id": "w1", "stock": 3, "reserve": 1},
                                        {"id": "w2", "stock": 4, "reserve": 1}]}]
    result = allocate([{"id": "p1", "qty": 2}, {"id": "p1", "qty": 2}], catalog)
    assert [(a.warehouse_id, a.qty) for a in result] == [("w1", 2), ("w2", 2)]
    with pytest.raises(StockAllocationError):
        allocate([{"id": "p1", "qty": 6}], catalog)
    with pytest.raises(StockAllocationError):
        allocate([{"id": "p1", "qty": 4}], catalog, ("w1",))


@pytest.mark.parametrize("entity", ["customerorder", "demand"])
def test_transport_guard_before_http(monkeypatch, entity):
    monkeypatch.setattr(settings, "external_writes_enabled", False)
    client = MoySkladClient.__new__(MoySkladClient)
    client.session = Mock()
    with pytest.raises(ExternalWritesDisabled):
        client.create_document(entity, {})
    client.session.post.assert_not_called()


def test_payloads_preserve_integer_money_and_link_split_documents():
    items = [{"id": "p1", "qty": 2, "price": 9999}]
    payload = customerorder("org", "cp", items, operation_key="order:1")
    assert payload["positions"][0]["price"] == 9999
    assert payload == customerorder("org", "cp", items, operation_key="order:1")
    first = demand("org", "cp", "w1", "co", items, "shipment:1")
    second = demand("org", "cp", "w2", "co", items, "shipment:2")
    assert first["syncId"] != second["syncId"]
    assert first["customerOrder"] == second["customerOrder"]
    assert first["store"] != second["store"]


@pytest.mark.parametrize("method", ["get_products", "get_stores", "get_stock_by_store", "get_organizations"])
def test_all_inventory_resources_are_paginated(method):
    client = MoySkladClient.__new__(MoySkladClient)
    client._get = Mock(side_effect=[{"rows": [{"id": "a"}], "meta": {"size": 2}},
                                   {"rows": [{"id": "b"}], "meta": {"size": 2}}])
    result = getattr(client, method)()
    assert (result["rows"] if isinstance(result, dict) else result) == [{"id": "a"}, {"id": "b"}]
    assert client._get.call_args_list[1].args[1]["offset"] == 1


def test_checkout_hash_normalizes_duplicate_lines():
    first = checkout_payload()
    second = {**first, "items": [{"product_id": "p1", "qty": 1}] * 2}
    assert request_hash(CheckoutCreate(**first)) == request_hash(CheckoutCreate(**second))


def test_public_checkout_contract_and_permissions(monkeypatch):
    from app.main import app
    from app.api import public
    monkeypatch.setattr(settings, "public_checkout_enabled", True)
    service = SimpleNamespace(submit=AsyncMock(return_value={"request_id": "k" * 16,
        "status": "needs_review", "total_minor": None, "message": "Review", "currency": "RUB"}))
    monkeypatch.setattr(public, "CheckoutService", lambda: service)
    code, body, _ = asyncio.run(request(app, "POST", "/api/v1/checkout", checkout_payload(), {"Idempotency-Key": "k" * 16}))
    assert code == 202 and body["total_minor"] is None
    code, _, _ = asyncio.run(request(app, "POST", "/api/v1/checkout", checkout_payload()))
    assert code == 422
    code, _, _ = asyncio.run(request(app, "POST", "/orders", checkout_payload()))
    assert code in {401, 503}


def test_cors_and_request_size_limits():
    from app.main import app
    code, _, headers = asyncio.run(request(app, "GET", "/", headers={"Origin": "https://evil.invalid"}))
    assert code == 200 and b"access-control-allow-origin" not in headers
    code, _, _ = asyncio.run(request(app, "POST", "/api/v1/checkout", {"x": "a" * 300000}))
    assert code == 413


def test_email_failure_does_not_advance_cursor():
    from tests.test_staging_readiness import RepeatingProvider, worker_message, MemoryCursorRepository, IdempotentPipeline
    from app.workers.email_ingestion import EmailIngestionWorker
    cursor = MemoryCursorRepository()
    worker = EmailIngestionWorker(RepeatingProvider([worker_message("bad")]), IdempotentPipeline("bad"), cursor)
    asyncio.run(worker.run_once())
    assert cursor.value is None


def test_gmail_attachment_text_never_becomes_order():
    import base64
    from app.integrations.email.gmail_provider import GmailEmailProvider
    encoded = base64.urlsafe_b64encode(b"Product x999").decode()
    assert GmailEmailProvider._body_text({"mimeType": "text/plain", "filename": "order.txt", "body": {"data": encoded}}) == ""
    assert GmailEmailProvider._body_text({"mimeType": "text/plain", "body": {"data": "a"}}) == ""


def test_existing_profile_has_priority_over_explicit_input():
    from app.services.customer_resolution_service import CustomerResolutionService
    from app.schemas.order import OrderCreate
    profile = SimpleNamespace(id=1, customer_type="retail", moysklad_counterparty_id=None, identities=[])
    repository = SimpleNamespace(find_by_identities=AsyncMock(return_value=[profile]),
                                 add_identities=AsyncMock(), update_customer_type=AsyncMock())
    order = OrderCreate(customer_name="Test", phone="1234567", customer_type="wholesale", items=[{"id": "p1", "qty": 1}])
    result = asyncio.run(CustomerResolutionService(repository).resolve(order))
    assert result.customer_type == "retail"
    repository.update_customer_type.assert_not_awaited()


def test_callback_buttons_include_revision_and_fit_telegram_limit():
    from app.services.draft_telegram_service import build_draft_keyboard
    draft = SimpleNamespace(id=4, revision=7, status="needs_review", counterparty_id=None,
                            counterparty_candidates=[], items=[])
    keyboard = build_draft_keyboard(draft)
    buttons = [b for row in keyboard["inline_keyboard"] for b in row]
    assert all(b["callback_data"].endswith(":v7") and len(b["callback_data"].encode()) <= 64 for b in buttons)


def test_production_requires_secure_configuration(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "internal_api_token", "")
    with pytest.raises(ValueError, match="INTERNAL_API_TOKEN"):
        settings.validate_runtime()


def test_staging_rejects_external_write_activation(monkeypatch):
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "external_writes_enabled", True)
    with pytest.raises(ValueError, match="staging"):
        settings.validate_runtime()
    from app.integrations.write_guard import require_external_writes
    with pytest.raises(ExternalWritesDisabled):
        require_external_writes()
