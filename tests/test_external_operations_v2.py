import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from app.config.settings import settings
from app.services.external_operation_service import ExternalOperationService, ExternalOperationConflict
from app.integrations.write_guard import ExternalWritesDisabled
from app.integrations.delivery import DeliveryDraft, CDEKDeliveryAdapter, YandexDeliveryAdapter, ManualCourierAdapter


class MemoryOperationStore:
    def __init__(self):
        self.operations = {}

    async def claim(self, key, payload):
        previous = self.operations.get(key)
        if previous:
            if previous["payload"] != payload or previous["status"] != "succeeded":
                raise ExternalOperationConflict("Reconciliation required")
            return previous["result"]
        self.operations[key] = {"payload": deepcopy(payload), "status": "inflight"}

    async def finish(self, key, result=None):
        self.operations[key].update(status="succeeded" if result else "uncertain", result=result)


def test_external_success_is_replayed_without_second_write(monkeypatch):
    monkeypatch.setattr(settings, "external_writes_enabled", True)
    send = AsyncMock(return_value={"id": "external-1"})
    service = ExternalOperationService(MemoryOperationStore())
    async def run():
        first = await service.execute("k", {"price": 9999}, send)
        assert await service.execute("k", {"price": 9999}, send) == first
        with pytest.raises(ExternalOperationConflict):
            await service.execute("k", {"price": 10000}, send)
    asyncio.run(run())
    send.assert_awaited_once()


@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError(), asyncio.CancelledError()])
def test_uncertain_external_result_never_retries_automatically(monkeypatch, failure):
    monkeypatch.setattr(settings, "external_writes_enabled", True)
    store = MemoryOperationStore()
    send = AsyncMock(side_effect=failure)
    service = ExternalOperationService(store)
    async def run():
        with pytest.raises(type(failure)):
            await service.execute("k", {}, send)
        with pytest.raises(ExternalOperationConflict):
            await service.execute("k", {}, send)
        assert store.operations["k"]["status"] == "uncertain"
    asyncio.run(run())
    send.assert_awaited_once()


def test_external_guard_precedes_database_or_transport(monkeypatch):
    monkeypatch.setattr(settings, "external_writes_enabled", False)
    store = SimpleNamespace(claim=AsyncMock())
    with pytest.raises(ExternalWritesDisabled):
        asyncio.run(ExternalOperationService(store).execute("k", {}, AsyncMock()))
    store.claim.assert_not_awaited()


def delivery(provider):
    return DeliveryDraft(shipment_id=1, provider=provider, idempotency_key="a" * 32,
        recipient_name="Test", recipient_phone="+79990000000", address="Test address",
        packages=[{"weight_grams": 100, "length_cm": 10, "width_cm": 10, "height_cm": 10}],
        provider_payload={"tariff_code": 136, "packages": [{"number": "1"}],
                          "route_points": [{"point_id": 1}], "items": [{"title": "Test"}]})


@pytest.mark.parametrize("adapter,provider", [(CDEKDeliveryAdapter, "cdek"), (YandexDeliveryAdapter, "yandex"), (ManualCourierAdapter, "manual")])
def test_delivery_guard_before_transport(monkeypatch, adapter, provider):
    monkeypatch.setattr(settings, "external_writes_enabled", False)
    with pytest.raises(ExternalWritesDisabled):
        asyncio.run(adapter().create(delivery(provider)))


def test_cdek_mock_adapter(monkeypatch):
    monkeypatch.setattr(settings, "external_writes_enabled", True)
    monkeypatch.setattr(settings, "delivery_cdek_client_id", "fake-id")
    monkeypatch.setattr(settings, "delivery_cdek_client_secret", "fake-secret")
    session = Mock()
    session.post.side_effect = [SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"access_token": "fake-token"}),
                               SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"entity": {"uuid": "delivery-1"}})]
    result = asyncio.run(CDEKDeliveryAdapter(session).create(delivery("cdek")))
    assert result["id"] == "delivery-1"
    assert session.post.call_count == 2


def test_yandex_mock_adapter_does_not_accept_claim_automatically(monkeypatch):
    monkeypatch.setattr(settings, "external_writes_enabled", True)
    monkeypatch.setattr(settings, "delivery_yandex_token", "fake-token")
    session = Mock()
    session.post.return_value = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": "claim-1"})
    assert asyncio.run(YandexDeliveryAdapter(session).create(delivery("yandex")))["id"] == "claim-1"
    session.post.assert_called_once()
    assert session.post.call_args.kwargs["params"]["request_id"] == "a" * 32


@pytest.mark.parametrize("adapter,provider", [(CDEKDeliveryAdapter, "cdek"), (YandexDeliveryAdapter, "yandex")])
def test_delivery_without_credentials_never_calls_transport(monkeypatch, adapter, provider):
    from app.integrations.delivery import DeliveryConfigurationError
    # Only in this network-blocked unit test, get past the write guard to test
    # the independent credential gate. No real runtime configuration changes.
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "external_writes_enabled", True)
    monkeypatch.setattr(settings, "delivery_cdek_client_id", "")
    monkeypatch.setattr(settings, "delivery_cdek_client_secret", "")
    monkeypatch.setattr(settings, "delivery_yandex_token", "")
    session = Mock()
    with pytest.raises(DeliveryConfigurationError):
        asyncio.run(adapter(session).create(delivery(provider)))
    session.post.assert_not_called()


def test_manual_courier_returns_local_dispatch_reference(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "external_writes_enabled", True)
    result = asyncio.run(ManualCourierAdapter().create(delivery("manual")))
    assert result == {"id": "manual:" + "a" * 32, "status": "awaiting_dispatch"}
