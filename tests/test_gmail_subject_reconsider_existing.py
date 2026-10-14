"""Recover already received DSD001 message from inquiry into a tentative order."""
import asyncio
from datetime import datetime, timezone

import pytest
from app.models.customer import Customer
from app.models.sales import CustomerType
from app.repositories.draft_order_repository import DraftOrderRepository
from app.services.draft_order_service import DraftOrderService, DraftOrderError
from app.services.draft_telegram_service import build_draft_keyboard, compact_draft_card
from app.services.product_matching_service import ProductMatchingService
from app.services.order_lifecycle import InvalidOrderTransitionError
from tests.test_tilda_sku_review import database, catalog, tilda_config


def test_existing_dsd_inquiry_recovered_without_second_message(database, monkeypatch):
    monkeypatch.setattr("app.repositories.draft_order_repository.async_session", database)
    repo=DraftOrderRepository()
    from types import SimpleNamespace
    catalog_provider=SimpleNamespace(get_products=lambda: [])
    service=DraftOrderService(repository=repo, matching_service=ProductMatchingService(
        provider=catalog_provider))
    async def run():
        async with database() as s, s.begin():
            customer=Customer(customer_type=CustomerType.WHOLESALE, display_name="Test wholesale")
            s.add(customer)
            await s.flush()
            cid=customer.id
        draft=await repo.create({
            "source":"email", "external_message_id":"existing-dsd-message-8",
            "sender_email":"origin@example.invalid","customer_name":"Кирилл Широков",
            "subject":"dsd 001 500мл",
            "body_text":"С уважением,\nКирилл Широков",
            "received_at":datetime.now(timezone.utc), "customer_id":cid,
            "customer_type":CustomerType.WHOLESALE,
            "contact_details":{"mail_intent":"inquiry"},
            "counterparty_id":"wholesale-cp",
            "counterparty_candidates":[], "status":"needs_review",
            "total":None, "problems":["Письмо-уточнение"], "items":[],
        })
        draft_id=draft.id
        draft=await repo.get(draft_id)
        assert not draft.items
        buttons=build_draft_keyboard(draft)["inline_keyboard"]
        labels=[b["text"] for row in buttons for b in row]
        assert "📦 Проверить как возможный заказ" in labels
        with pytest.raises(InvalidOrderTransitionError):
            await repo.classify_possible_email(draft_id,"order",999)
        recovered=await service.reconsider_inquiry_subject(draft_id,111)
        assert recovered.contact_details["mail_intent"]=="possible_order"
        assert recovered.contact_details["reconsidered_by_telegram_id"]==111
        assert len(recovered.items)==1
        assert recovered.items[0].raw_product_text=="dsd 001 500мл"
        assert recovered.items[0].qty==0
        assert recovered.status=="needs_review"
        assert recovered.total is None
        text=compact_draft_card(recovered)
        assert "Возможный заказ" in text and "количество не указано" in text
        options=[x["text"] for row in build_draft_keyboard(recovered)["inline_keyboard"] for x in row]
        assert "✅ Это заказ" in options and "💬 Это вопрос" in options
        with pytest.raises(DraftOrderError):
            await service.reconsider_inquiry_subject(draft_id,111)
        with pytest.raises(InvalidOrderTransitionError):
            await repo.classify_possible_email(draft_id,"order",999)
        confirmed=await service.classify_possible_email(draft_id,"order",111)
        assert confirmed.contact_details["mail_intent"]=="order"
        assert confirmed.status=="needs_review"
        assert len(confirmed.items)==1
        assert confirmed.items[0].qty==0
        with pytest.raises(DraftOrderError):
            await service.finalize(draft_id)
        assert len((await repo.list(source="email"))) == 1
    asyncio.run(run())


def test_reclassify_as_question_retains_original_email(database,monkeypatch):
    monkeypatch.setattr("app.repositories.draft_order_repository.async_session", database)
    repo=DraftOrderRepository()
    async def run():
        async with database() as s, s.begin():
            cust=Customer(customer_type=CustomerType.WHOLESALE,display_name="Client")
            s.add(cust)
            await s.flush()
            cid=cust.id
        draft=await repo.create({
            "source":"email", "external_message_id":"subject-ambiguous-question-9",
            "sender_email":"buyer@example.invalid","customer_name":"Алексей",
            "subject":"dsd 001 500мл", "body_text":"Спасибо, Алексей",
            "received_at":datetime.now(timezone.utc),
            "customer_id":cid,"customer_type":CustomerType.WHOLESALE,
            "contact_details":{"mail_intent":"possible_order"},
            "counterparty_id":None,"counterparty_candidates":[],"status":"needs_review",
            "total":None,"problems":["Нужна проверка"], "items":[{
                "raw_product_text":"dsd 001 500мл","qty":0,
                "quantity_confidence":"unknown","match_status":"not_found",
                "product_id":None,"price":None,"item_total":None,
                "candidates":[]}],
        })
        obj=await repo.classify_possible_email(draft.id,"inquiry",111)
        assert obj.contact_details["mail_intent"]=="inquiry"
        reread=await repo.get(draft.id)
        assert reread.inbound_message.body_text=="Спасибо, Алексей"
        assert reread.contact_details["mail_intent"]=="inquiry"
        with pytest.raises(InvalidOrderTransitionError):
            await repo.finalize(draft.id)
    asyncio.run(run())
