"""Pre-order source intent. This never creates stock, reservations or purchases."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database.session import async_session
from app.models.draft_order import DraftOrder
from app.models.order_desk import OrderDesk
from app.models.inbound_message import InboundMessage
from app.services.order_desk import authorized_manager, require_owner, audit, DeskError
from app.services.tilda_review import source_rows, amounts


async def record(draft_id, actor, chat_id, revision, *, item_id=None, source=None):
    async with async_session() as session, session.begin():
        await authorized_manager(session, actor, chat_id)
        draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id)
            .options(selectinload(DraftOrder.items)).with_for_update())
        desk = await session.get(OrderDesk, draft_id, with_for_update=True)
        if not draft or not desk or desk.order_id or draft.status in {"new", "rejected"}:
            raise DeskError("Заявка недоступна.")
        require_owner(desk, actor)
        if draft.revision != revision:
            raise DeskError("Карточка устарела. Откройте актуальную заявку.")
        inbound = await session.get(InboundMessage, draft.inbound_message_id)
        payload, items, bindings = source_rows(draft, desk, inbound)
        problems, total = amounts(payload)
        if problems or any(i.qty != row["qty"] or i.price != row.get("price_minor") or i.raw_product_text != row["name"]
                for i, row in zip(items, payload["items"], strict=True)):
            raise DeskError("Исходные цены и количество требуют проверки.")
        contact = dict(draft.contact_details)
        if item_id is None:
            contact["tilda_source_data_confirmed"] = True
            audit(session, desk, "source_data_confirmed", actor, total_minor=total)
        else:
            if not contact.get("tilda_source_data_confirmed"):
                raise DeskError("Сначала подтвердите исходные позиции и сумму.")
            if source not in {"own", "own_x", "external"} or item_id not in {i.id for i in items}:
                raise DeskError("Источник или позиция недоступны.")
            intents = dict(contact.get("tilda_source_intents", {}))
            intents[str(item_id)] = {"source": source, "status": "identity_required" if not
                next(i for i in items if i.id == item_id).product_id else "availability_required"}
            contact["tilda_source_intents"] = intents
            audit(session, desk, "source_intent_selected", actor, item_id=item_id, source=source)
        draft.contact_details = contact
        draft.revision += 1


async def bind(draft_id, item_id, product_id, actor, chat_id, revision):
    from app.services.product_matching_service import ProductMatchingService
    matcher = ProductMatchingService()
    matcher.async_gateway.invalidate_products()
    product = next((p for p in await matcher.products_async() if p["id"] == product_id), None)
    if not product:
        raise DeskError("Товар недоступен. Повторите поиск.")
    async with async_session() as session, session.begin():
        await authorized_manager(session, actor, chat_id)
        draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id)
            .options(selectinload(DraftOrder.items)).with_for_update())
        desk = await session.get(OrderDesk, draft_id, with_for_update=True)
        if not desk or not draft or desk.order_id or draft.status in {"new", "rejected"} or draft.revision != revision:
            raise DeskError("Заявка изменилась. Повторите поиск.")
        require_owner(desk, actor)
        item = next((i for i in draft.items if i.id == item_id), None)
        if not item or product_id not in {c["id"] for c in item.candidates}:
            raise DeskError("Вариант товара устарел.")
        bindings = dict(draft.contact_details.get("tilda_manual_products", {}))
        bindings[str(item_id)] = product_id
        item.product_id, item.product_name, item.article = product_id, product.get("name"), product.get("article")
        item.match_status = "matched"
        intents = dict(draft.contact_details.get("tilda_source_intents", {}))
        if str(item_id) in intents:
            intents[str(item_id)] = {**intents[str(item_id)], "status": "availability_required"}
        draft.contact_details = {**draft.contact_details, "tilda_manual_products": bindings, "tilda_source_intents": intents}
        draft.revision += 1
        audit(session, desk, "product_manually_bound", actor, item_id=item_id, product_id=product_id)


async def search(draft_id, item_id, query, actor, chat_id, revision):
    from app.services.product_matching_service import ProductMatchingService
    from dataclasses import asdict
    match = await ProductMatchingService().match_async(query[:500], 1)
    candidates = [asdict(c) for c in match.candidates]
    if match.product:
        candidates = [{"id": match.product["id"], "name": match.product.get("name"), "score": 1}]
    async with async_session() as session, session.begin():
        await authorized_manager(session, actor, chat_id)
        draft = await session.scalar(select(DraftOrder).where(DraftOrder.id == draft_id)
            .options(selectinload(DraftOrder.items)).with_for_update())
        desk = await session.get(OrderDesk, draft_id, with_for_update=True)
        if not desk or not draft or desk.order_id or draft.status in {"new", "rejected"} or draft.revision != revision:
            raise DeskError("Заявка изменилась. Откройте поиск заново.")
        require_owner(desk, actor)
        item = next((i for i in draft.items if i.id == item_id), None)
        if not item:
            raise DeskError("Позиция не найдена.")
        item.candidates = candidates
        draft.revision += 1
        return draft
