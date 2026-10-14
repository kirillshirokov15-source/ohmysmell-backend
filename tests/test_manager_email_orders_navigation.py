"""Customer Gmail intake is listed and notified in manager Telegram; Buying remains separate."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot import telegram_bot
from app.bot.manager_dialog import fallback
from app.services.draft_telegram_service import compact_draft_card


class FakeMessage:
    def __init__(self):
        self.sent = []

    async def answer(self, message, *, reply_markup=None, **kwargs):
        self.sent.append((message, reply_markup))


def _email_draft(identifier=11):
    items = [SimpleNamespace(id=1, raw_product_text="Marvis Strong Mint 85 ml", qty=2,
                             quantity_confidence="confirmed", candidates=[]),
             SimpleNamespace(id=2, raw_product_text="Chanel Allure Homme Sport", qty=1,
                             quantity_confidence="confirmed", candidates=[])]
    return SimpleNamespace(id=identifier, source="email", status="needs_review",
        customer_name="Магазин Косметики", sender_email="buyer@example.invalid",
        items=items, total=None, contact_details={}, revision=1, counterparty_candidates=[],
        counterparty_id=None)


def test_manager_more_menu_contains_dedicated_wholesale_gmail_route():
    more = fallback(777, more=True)
    callbacks = [b.callback_data for row in more.inline_keyboard for b in row]
    labels = [b.text for row in more.inline_keyboard for b in row]
    assert "menu:777:email_orders" in callbacks
    assert "📨 Заказы с почты" in labels
    assert "menu:777:procurements" in callbacks
    assert "menu:777:drafts" in callbacks


def test_email_alert_card_shows_origins_and_original_lines():
    email = _email_draft()
    card = compact_draft_card(email)
    assert "Новый оптовый заказ из почты" in card
    assert "Магазин Косметики" in card
    assert "Marvis Strong Mint 85 ml" in card
    assert "2 шт." in card
    assert "Chanel Allure Homme Sport" in card
    assert "требует проверки" in card
    site = SimpleNamespace(**{**email.__dict__, "source":"website"})
    assert "Новый оптовый заказ из почты" not in compact_draft_card(site)


def test_paginated_email_view_never_queries_website_or_buying(monkeypatch):
    rows = [_email_draft(i) for i in range(1, 11)]
    list_mock = AsyncMock(return_value=rows)
    monkeypatch.setattr(telegram_bot.DraftOrderRepository, "list", list_mock)
    msg = FakeMessage()

    async def run():
        await telegram_bot.show_email_order_list(msg, 0)
        assert list_mock.await_args.kwargs == {
            "limit":10, "offset":0, "source":"email", "active_only":False}
        body, keys = msg.sent[0]
        assert "Оптовые заявки из почты" in body
        assert len(keys.inline_keyboard) == 11
        assert keys.inline_keyboard[0][0].callback_data == "draft:refresh:1"
        assert keys.inline_keyboard[-1][0].callback_data == "draftpage:email:10"
        list_mock.return_value = [_email_draft(11)]
        await telegram_bot.show_email_order_list(msg, 10)
        assert keys is not msg.sent[-1][1]
        assert msg.sent[-1][1].inline_keyboard[-1][0].callback_data == "draftpage:email:0"

    asyncio.run(run())


def test_email_orders_with_empty_mailbox_are_explanatory(monkeypatch):
    list_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(telegram_bot.DraftOrderRepository, "list", list_mock)
    msg = FakeMessage()

    async def run():
        await telegram_bot.show_email_order_list(msg, 0)
        assert len(msg.sent) == 1
        assert "пока нет" in msg.sent[0][0]
        assert not msg.sent[0][1].inline_keyboard

    asyncio.run(run())
