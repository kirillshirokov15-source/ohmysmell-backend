"""Provider-neutral delivery boundary. Credentials do not change the order core."""
import asyncio
from typing import Literal, Protocol
from pydantic import BaseModel, ConfigDict, Field
from app.integrations.write_guard import require_external_writes
from app.integrations.http_tls import verified_session
from app.config.settings import settings


class DeliveryConfigurationError(RuntimeError):
    pass


class Package(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weight_grams: int = Field(strict=True, gt=0)
    length_cm: int = Field(strict=True, gt=0)
    width_cm: int = Field(strict=True, gt=0)
    height_cm: int = Field(strict=True, gt=0)


class DeliveryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shipment_id: int = Field(gt=0)
    provider: Literal["cdek", "yandex", "manual"]
    idempotency_key: str = Field(min_length=16, max_length=128)
    recipient_name: str = Field(min_length=1, max_length=255)
    recipient_phone: str = Field(min_length=7, max_length=32)
    address: str = Field(min_length=1, max_length=1000)
    packages: list[Package] = Field(min_length=1, max_length=100)
    # Provider tariff, pickup point and coordinates are supplied by a future
    # delivery quotation flow, never inferred from order prices or warehouse IDs.
    provider_payload: dict = Field(default_factory=dict)


class DeliveryAdapter(Protocol):
    async def create(self, draft: DeliveryDraft) -> dict: ...


class ManualCourierAdapter:
    async def create(self, draft):
        require_external_writes()
        return {"id": "manual:" + draft.idempotency_key, "status": "awaiting_dispatch"}


class YandexDeliveryAdapter:
    def __init__(self, session=None):
        self.session = session or verified_session()

    async def create(self, draft):
        require_external_writes()
        if not settings.delivery_yandex_token:
            raise DeliveryConfigurationError("YANDEX_DELIVERY_TOKEN не настроен")
        if not draft.provider_payload.get("route_points") or not draft.provider_payload.get("items"):
            raise DeliveryConfigurationError("Нужен проверенный маршрут и параметры доставки")
        def send():
            require_external_writes()
            response = self.session.post("https://b2b.taxi.yandex.net/b2b/cargo/integration/v2/claims/create",
                params={"request_id": draft.idempotency_key}, json=draft.provider_payload,
                headers={"Authorization": "Bearer " + settings.delivery_yandex_token}, timeout=(5, 30))
            response.raise_for_status()
            return response.json()
        return await asyncio.to_thread(send)


class CDEKDeliveryAdapter:
    def __init__(self, session=None):
        self.session = session or verified_session()

    async def create(self, draft):
        require_external_writes()
        if not settings.delivery_cdek_client_id or not settings.delivery_cdek_client_secret:
            raise DeliveryConfigurationError("Credentials СДЭК не настроены")
        if not draft.provider_payload.get("tariff_code") or not draft.provider_payload.get("packages"):
            raise DeliveryConfigurationError("Нужен проверенный тариф и упаковка СДЭК")
        def send():
            require_external_writes()
            token = self.session.post("https://api.cdek.ru/v2/oauth/token", data={
                "grant_type": "client_credentials", "client_id": settings.delivery_cdek_client_id,
                "client_secret": settings.delivery_cdek_client_secret}, timeout=(5, 30))
            token.raise_for_status()
            response = self.session.post("https://api.cdek.ru/v2/orders", json={
                **draft.provider_payload, "number": draft.idempotency_key}, headers={
                    "Authorization": "Bearer " + token.json()["access_token"]}, timeout=(5, 30))
            response.raise_for_status()
            data = response.json()
            return {"id": data.get("entity", {}).get("uuid"), "status": "submitted"}
        return await asyncio.to_thread(send)
