"""Explicit, guarded creation using the existing durable operation journal."""
import asyncio
import hashlib
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database.session import async_session
from app.models.draft_order import DraftOrder
from app.models.order_desk import OrderDesk
from app.models.inbound_message import InboundMessage
from app.models.manager import Manager
from app.services.order_desk import authorized_manager, require_owner, DeskError, audit
from app.services.external_operation_service import ExternalOperationService
from app.integrations.moysklad.client import MoySkladClient
from app.integrations.write_guard import require_external_writes
from app.services.counterparty_matching_service import CounterpartyMatchingService
from app.services.product_matching_service import ProductMatchingService
from app.services.tilda_review import source_rows


async def create(entity, payload, key):
    async def send(body):
        result = await asyncio.to_thread(MoySkladClient().create_directory_entry, entity, body)
        if result.get("archived") or any(result.get(k) != v for k, v in body.items()):
            raise DeskError("Создание справочника требует сверки.")
        return result
    return await ExternalOperationService().execute(key, payload, send)


async def create_counterparty(draft_id, actor, revision):
    require_external_writes()
    async with async_session() as session, session.begin():
        if not await session.scalar(select(Manager.id).where(Manager.telegram_id == actor, Manager.is_active.is_(True))):
            raise DeskError("Нет доступа.")
        draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id).with_for_update())
        if not draft or draft.revision != revision or draft.finalized_order_id or draft.counterparty_id:
            raise DeskError("Заявка изменилась.")
        from app.repositories.draft_order_repository import DraftOrderRepository
        DraftOrderRepository._ensure_editable(draft)
        contact = draft.contact_details or {}
        candidates = await CounterpartyMatchingService().candidates_async(contact.get("inn"), draft.sender_email,
            contact.get("phone"), draft.customer_name)
        if candidates:
            raise DeskError("Найдены возможные дубли. Выберите контрагента после повторного поиска.")
        if not draft.customer_name:
            raise DeskError("Сначала укажите название клиента.")
        key = "counterparty:draft:" + str(draft_id)
        body = {"name": draft.customer_name, "externalCode": key}
        if draft.sender_email:
            body["email"] = draft.sender_email
        for field in ("phone", "inn"):
            if contact.get(field):
                body[field] = contact[field]
        created = await create("counterparty", body, key)
        draft.counterparty_candidates = [{**CounterpartyMatchingService._candidate(created),
            "inn": created.get("inn"), "reason": "создан по подтверждению менеджера"}]
        draft.revision += 1
        # Deliberately still require the normal fresh-read selection before linking.
        return draft.id


async def create_product(draft_id, item_id, actor, chat_id, revision):
    require_external_writes()
    async with async_session() as session, session.begin():
        await authorized_manager(session, actor, chat_id)
        draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id)
            .options(selectinload(DraftOrder.items)).with_for_update())
        desk = await session.get(OrderDesk, draft_id, with_for_update=True)
        if not draft or not desk or desk.order_id or draft.status in {"new", "rejected"} or draft.revision != revision:
            raise DeskError("Заявка изменилась.")
        require_owner(desk, actor)
        payload, items, _ = source_rows(draft, desk, await session.get(InboundMessage, draft.inbound_message_id))
        item = next((i for i in items if i.id == item_id), None)
        if not item or item.product_id or draft.contact_details.get("tilda_source_intents", {}).get(str(item_id), {}).get("source") != "own_x":
            raise DeskError("Создание доступно для подтверждённого собственного товара X.")
        original = payload["items"][items.index(item)]
        if not original["externalid"].strip():
            raise DeskError("В исходных данных нет SKU. Сопоставьте существующий товар вручную.")
        matcher = ProductMatchingService()
        matcher.async_gateway.invalidate_products()
        products = await matcher.products_async()
        if matcher.match_sku(products, original["externalid"], item.qty).status != "not_found":
            raise DeskError("SKU уже существует. Повторите сопоставление.")
        if matcher._match(products, original["name"], item.qty).status != "not_found":
            raise DeskError("Найден похожий товар. Проверьте варианты перед созданием.")
        key = "product:sku:" + hashlib.sha256(original["externalid"].encode()).hexdigest()[:48]
        created = await create("product", {"name": original["name"], "article": original["externalid"], "externalCode": key}, key)
        item.candidates = [{"id": created["id"], "name": created["name"], "article": created.get("article")}]
        draft.revision += 1
        matcher.async_gateway.invalidate_products()
        audit(session, desk, "product_created", actor, item_id=item_id, product_id=created["id"])
        return draft.revision
