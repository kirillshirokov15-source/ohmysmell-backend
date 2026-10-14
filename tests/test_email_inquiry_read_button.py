"""Gmail inquiry text must survive storage and appear on manager button press.

All tests use local SQLite, fake Telegram callbacks, and no external API calls.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models.customer import Customer
from app.models.sales import CustomerType
from app.repositories.draft_order_repository import DraftOrderRepository
from app.services.draft_telegram_service import (
    build_draft_card, build_inquiry_message, compact_draft_card)
from app.bot import telegram_bot
from tests.test_tilda_sku_review import database, catalog, tilda_config


INQUIRY = ("Здравствуйте! Подскажите, есть ли Marvis Classic Strong Mint "
           "85 ml в наличии и сколько он стоит?\n\nС уважением, Кирилл Широков")


def test_inquiry_original_body_loaded_from_persisted_inbound_message(database, monkeypatch):
    monkeypatch.setattr("app.repositories.draft_order_repository.async_session", database)
    repo = DraftOrderRepository()

    async def scenario():
        async with database() as s, s.begin():
            cust = Customer(customer_type=CustomerType.WHOLESALE, display_name="Клиент")
            s.add(cust)
            await s.flush()
            cid = cust.id
        new = await repo.create({
            "source": "email",
            "external_message_id": "unique-inquiry-01",
            "sender_email": "client@example.invalid",
            "customer_name": "Кирилл Широков",
            "subject": "OMS LIVE TEST 20261014 735126",
            "body_text": INQUIRY,
            "received_at": datetime.now(timezone.utc),
            "customer_id": cid,
            "customer_type": CustomerType.WHOLESALE,
            "contact_details": {"mail_intent": "inquiry"},
            "counterparty_id": None,
            "counterparty_candidates": [],
            "status": "needs_review",
            "total": None,
            "problems": ["Требуется ответить клиенту"],
            "items": [],
        })
        assert new.inbound_message.body_text == INQUIRY
        assert INQUIRY.splitlines()[0] in build_inquiry_message(new)

        loaded = await repo.get(new.id)
        assert loaded.inbound_message.body_text == INQUIRY
        assert "Marvis Classic Strong Mint" in compact_draft_card(loaded)
        assert "сколько он стоит?" in build_draft_card(loaded)
        direct = build_inquiry_message(loaded)
        assert "client@example.invalid" in direct
        assert "Не заказ" in direct
        assert INQUIRY in direct
        listed = await repo.list(source="email")
        assert len(listed) == 1
        assert INQUIRY == listed[0].inbound_message.body_text
        return loaded

    result = asyncio.run(scenario())
    assert result.inbound_message.body_text == INQUIRY


def test_click_read_question_sends_new_message_instead_of_edit(monkeypatch):
    text = INQUIRY
    draft = SimpleNamespace(
        id=7,
        source="email",
        contact_details={"mail_intent": "inquiry"},
        subject="Уточнение цены",
        sender_email="client@example.invalid",
        inbound_message=SimpleNamespace(body_text=text),
        items=[],
    )
    monkeypatch.setattr(telegram_bot, "safe_callback_answer", AsyncMock(return_value=True))
    monkeypatch.setattr(telegram_bot.manager_repository, "is_active_by_telegram_id",
                        AsyncMock(return_value=True))
    monkeypatch.setattr(telegram_bot.DraftOrderRepository, "get",
                        AsyncMock(return_value=draft))
    # No MoySklad client is needed to read an inquiry.
    monkeypatch.setattr(telegram_bot, "DraftOrderService",
                        lambda: SimpleNamespace())
    reply = AsyncMock()
    cb = SimpleNamespace(
        data="draft:details:7",
        from_user=SimpleNamespace(id=111),
        message=SimpleNamespace(answer=reply),
    )
    async def scenario():
        await telegram_bot.draft_callback_handler(cb)
    asyncio.run(scenario())
    reply.assert_awaited_once()
    args, kw = reply.await_args
    assert "Marvis Classic Strong Mint" in args[0]
    assert kw["parse_mode"] is None
