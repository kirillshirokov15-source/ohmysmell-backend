"""Regression for the formerly silent 'Сверить контрагентов' button."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from app.bot import telegram_bot


def test_counterparty_button_always_returns_explanatory_message(monkeypatch):
    repo=SimpleNamespace(
        id=8, source="email", status="needs_review",
        items=[], counterparty_candidates=[], counterparty_id=None,
        contact_details={"mail_intent":"order"}, customer_type="wholesale",
        sender_email="owner@example.invalid",customer_name="Person",
        revision=9, total=None, problems=[], subject="Order", counterparty_name=None)
    answer=AsyncMock()
    edit=AsyncMock()
    cb=SimpleNamespace(
        data="draft:counterparty_select:8:v9",
        from_user=SimpleNamespace(id=234),
        message=SimpleNamespace(answer=answer, edit_text=edit))
    monkeypatch.setattr(telegram_bot,"safe_callback_answer", AsyncMock(return_value=True))
    monkeypatch.setattr(telegram_bot.manager_repository,"is_active_by_telegram_id",AsyncMock(return_value=True))
    svc=SimpleNamespace(
        repository=SimpleNamespace(expected_revision=9),
        load_counterparty_candidates=AsyncMock(return_value=repo))
    monkeypatch.setattr(telegram_bot,"DraftOrderService",lambda:svc)
    asyncio.run(telegram_bot.draft_callback_handler(cb))
    answer.assert_awaited_once()
    text=answer.await_args.args[0]
    assert "Точных совпадений не найдено" in text
    assert "Ввести контрагента вручную" in text
    assert "Создание новой карточки" in text
    assert "/cp" not in text
    edit.assert_not_awaited()
