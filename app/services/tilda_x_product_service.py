"""Explicitly catalog a Tilda-only X product without affecting inventory.

The item is NOT received, reserved, or shipped here. An authenticated manager
creates a zero-stock MoySklad product record from an immutable Tilda webhook
SKU and subsequently selects X only at the normal allocation step.
"""
import asyncio
import hashlib
import re

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config.settings import settings
from app.database.session import async_session
from app.models.draft_order import DraftOrder
from app.models.order_desk import OrderDesk
from app.models.inbound_message import InboundMessage
from app.services.order_desk import (
    DeskService, DeskError, authorized_manager, require_owner, source_rows,
)
from app.services.product_matching_service import ProductMatchingService
from app.services.external_operation_service import (
    ExternalOperationService, ExternalOperationConflict,
)
from app.integrations.moysklad.async_gateway import catalog_cache
from app.integrations.moysklad.client import MoySkladClient


def x_product_key(sku):
    if not isinstance(sku, str) or not 1 <= len(sku.strip()) <= 255:
        raise DeskError("Нет исходного артикула Tilda для безопасного создания товара.")
    if any(ord(c) < 32 for c in sku):
        raise DeskError("Некорректный артикул Tilda.")
    return hashlib.sha256(sku.strip().encode("utf-8")).hexdigest()[:32]


def x_product_payload(row):
    name = " ".join((row.get("name") or "").split())
    sku = (row.get("externalid") or "").strip()
    token = x_product_key(sku)
    if not 3 <= len(name) <= 255:
        raise DeskError("Название Tilda должно быть длиной 3–255 символов.")
    return {
        "name": name, "article": sku, "externalCode": f"oms-tilda-x-{token}",
        "description": "OhMySmell / Tilda / warehouse X: карточка до физической приёмки; без остатков.",
    }


class TildaXProductService:
    def __init__(self, *, sessions=None, client=None, operations=None):
        self.sessions = sessions or async_session
        self.client = client or MoySkladClient()
        self.operations = operations or ExternalOperationService()

    async def _draft(self, draft_id, position, actor, chat_id):
        if type(position) is not int or position < 1:
            raise DeskError("Некорректная позиция заказа.")
        async with self.sessions() as session:
            await authorized_manager(session, actor, chat_id)
            desk = await session.get(OrderDesk, draft_id)
            draft = await session.scalar(select(DraftOrder)
                .where(DraftOrder.id == draft_id).options(selectinload(DraftOrder.items)))
            if not draft or not desk or draft.source != "website" or (
                    draft.status == "rejected" or desk.order_id or draft.finalized_order_id):
                raise DeskError("Сопоставление доступно только для новой заявки Tilda.")
            require_owner(desk, actor)
            inbound = await session.get(InboundMessage, draft.inbound_message_id)
            try:
                payload, ordered, bindings = source_rows(draft, desk, inbound)
            except (KeyError, ValueError, TypeError) as error:
                raise DeskError("Исходная заявка Tilda изменилась; создание товара запрещено.") from error
            if position > len(ordered):
                raise DeskError("Позиция заявки не существует.")
            item = ordered[position - 1]
            if item.product_id or item.match_status == "matched":
                raise DeskError("Этот товар уже сопоставлен с МойСклад.")
            source_index = bindings[str(item.id)]
            row = payload["items"][source_index]
            new_product = x_product_payload(row)
            return {
                "revision": draft.revision, "sku": new_product["article"],
                "name": new_product["name"], "price_minor": row.get("price_minor"),
                "qty": row.get("qty"), "payload": new_product,
                "position": position, "item_id": item.id,
            }

    @staticmethod
    def _find_collisions(products, payload):
        normalize = ProductMatchingService.normalize
        article, name, code = (normalize(payload[k]) for k in ("article", "name", "externalCode"))
        return [p for p in products if (
            normalize(p.get("article")) == article or
            normalize(p.get("code")) == article or
            normalize(p.get("externalCode")) == code or
            normalize(p.get("name")) == name
        )]

    @staticmethod
    def _verified_product(product, payload):
        return bool(product and product.get("id") and not product.get("archived") and
            product.get("name") == payload["name"] and
            product.get("article") == payload["article"] and
            product.get("externalCode") == payload["externalCode"])

    async def preview(self, draft_id, position, actor, chat_id):
        data = await self._draft(draft_id, position, actor, chat_id)
        products = await asyncio.to_thread(self.client.get_products)
        collisions = self._find_collisions(products, data["payload"])
        if collisions:
            raise DeskError(
                "В МойСклад уже есть карточка с таким артикулом, названием или кодом. "
                "Не создаём дубль – найдите существующую карточку вручную.")
        return data

    async def create_and_bind(self, draft_id, position, actor, chat_id, revision):
        from app.integrations.write_guard import require_external_writes
        import os
        require_external_writes()
        if settings.environment != "production" or (
                os.getenv("OMS_OWN_X_PRODUCT_CREATE_ENABLED", "false").lower() != "true"):
            raise DeskError("Создание новых карточек X не включено в production.")
        data = await self._draft(draft_id, position, actor, chat_id)
        if data["revision"] != revision:
            raise DeskError("Заявка изменилась. Откройте актуальную карточку.")
        payload = data["payload"]
        token = x_product_key(payload["article"])
        key = f"oms:tilda:xproduct:{token}"
        # A fresh provider lookup happens even when a prior creation attempt
        # completed with a timeout, so it cannot be posted a second time.
        products = await asyncio.to_thread(self.client.get_products)
        collisions = self._find_collisions(products, payload)
        matching = [p for p in collisions if self._verified_product(p, payload)]
        if collisions and (len(collisions) != 1 or len(matching) != 1):
            raise DeskError("В МойСклад уже есть похожий товар, но идентичность не подтверждена. "
                            "Создание дубля запрещено.")
        if matching:
            product = matching[0]
        else:
            try:
                product = await self.operations.execute(
                    key, {"entity": "product", "document": payload},
                    lambda intent: asyncio.to_thread(
                        self.client.create_x_catalog_product, intent["document"]))
            except ExternalOperationConflict as error:
                raise DeskError(
                    "Создание могло уже пройти в МойСклад. "
                    "Обновите заявку и сверьте товар по точному артикулу, "
                    "не создавайте карточку повторно.") from error
        # Only the unarchived, freshly read, exact catalogue identity can be
        # bound; no fallback to fuzzy matches or suggested other variants.
        try:
            verified = await asyncio.to_thread(self.client.get_product, product["id"])
        except Exception as error:
            raise DeskError("Карточка создана, но её нельзя проверить в МойСклад. "
                            "Не создавайте вторую; сначала проверьте каталог.") from error
        if not self._verified_product(verified, payload):
            raise DeskError("Товар МойСклад не совпал с исходным артикулом Tilda. "
                            "Автоматическая привязка запрещена.")
        catalog_cache.invalidate()
        result = await DeskService().bind_site_product(
            draft_id, actor, chat_id, position, revision, verified["id"])
        return result
