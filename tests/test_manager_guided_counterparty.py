"""Guarded inline counterparty search: transliteration gives suggestions, not identity."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.counterparty_matching_service import (
    CounterpartyMatchingService, normalize_text, search_aliases,
)


def test_aliases_transliterate_davian_and_ignore_case_and_whitespace():
    assert "давиан" in search_aliases("  DAVIAN  ")
    assert "davian" in search_aliases(" Давиан ")
    assert "ооо давиан" in search_aliases(" ООО  Давиан ")
    assert search_aliases("1234567890") == ("1234567890",)


def test_transliterated_manual_matches_never_become_verified_identity():
    record = {"id": "myuuid-1", "name": "Давиан", "inn": "1234567890",
              "email": "someone@example.invalid", "archived": False}
    lookups = []
    async def search(query):
        lookups.append(query)
        return [record] if query == "давиан" else []
    service = CounterpartyMatchingService(provider=SimpleNamespace())
    service.async_gateway = SimpleNamespace(search_counterparties=search)
    matches = asyncio.run(service.manual_search_async("DAVIAN"))
    assert "давиан" in lookups
    assert matches[0]["id"] == "myuuid-1"
    assert matches[0]["reason"].startswith("Поиск менеджера")
    assert service.evidence(record, name="DAVIAN") is None


def test_manual_search_requires_valid_input_and_limits_results():
    service = CounterpartyMatchingService(provider=SimpleNamespace())
    async def search(query):
        return [{"id": str(n), "name": f"Давиан {n}", "archived": False} for n in range(20)]
    service.async_gateway = SimpleNamespace(search_counterparties=search)
    assert len(asyncio.run(service.manual_search_async("Давиан"))) == 5
    with pytest.raises(ValueError):
        asyncio.run(service.manual_search_async("a"))


def test_force_reply_only_consumes_reply_for_same_manager_and_prompt(monkeypatch):
    from app.bot import manager_inputs
    key = (-100700, 334)
    messages = []
    draft = SimpleNamespace(id=75, revision=9, status="needs_review",
        finalized_order_id=None, source="email", counterparty_id=None)
    service = SimpleNamespace(
        _get_required=AsyncMock(return_value=draft),
        repository=SimpleNamespace(expected_revision=None),
        search_counterparties=AsyncMock(return_value=draft))
    monkeypatch.setattr("app.services.draft_order_service.DraftOrderService", lambda: service)
    monkeypatch.setattr("app.services.draft_telegram_service.build_draft_card",
                        lambda _: "Заявка 75")
    monkeypatch.setattr("app.services.draft_telegram_service.build_draft_keyboard",
                        lambda _: {"inline_keyboard": []})
    async def answer(text, **kwargs):
        messages.append((text, kwargs))
    msg = SimpleNamespace(
        chat=SimpleNamespace(id=key[0]),
        from_user=SimpleNamespace(id=key[1]),
        reply_to_message=SimpleNamespace(message_id=99),
        message_id=100,
        text="DAVIAN",
        answer=answer)
    manager_inputs.pending[key] = (time.monotonic()+60, 99, "cpmanual",
                                   {"draft_id": 75, "revision": 9})
    async def allow(message):
        return True
    try:
        assert asyncio.run(manager_inputs.route(msg, allow))
        service.search_counterparties.assert_awaited_once_with(75, "DAVIAN")
        assert key not in manager_inputs.pending
        assert any("Заявка" in m[0] for m in messages)
    finally:
        manager_inputs.pending.pop(key, None)


def test_stale_force_reply_does_not_run_search(monkeypatch):
    from app.bot import manager_inputs
    key = (-100700, 335)
    draft = SimpleNamespace(id=75, revision=10, status="needs_review",
        finalized_order_id=None, source="email", counterparty_id=None)
    service = SimpleNamespace(_get_required=AsyncMock(return_value=draft),
        search_counterparties=AsyncMock())
    monkeypatch.setattr("app.services.draft_order_service.DraftOrderService", lambda: service)
    answers = []
    async def answer(text, **kwargs):
        answers.append(text)
    msg = SimpleNamespace(chat=SimpleNamespace(id=key[0]),
        from_user=SimpleNamespace(id=key[1]),
        reply_to_message=SimpleNamespace(message_id=99),
        text="DAVIAN", answer=answer)
    manager_inputs.pending[key] = (time.monotonic()+60, 99, "cpmanual",
                                   {"draft_id": 75, "revision": 9})
    try:
        assert asyncio.run(manager_inputs.route(msg, AsyncMock(return_value=True)))
        service.search_counterparties.assert_not_awaited()
        assert key not in manager_inputs.pending
        assert "изменилась" in answers[0]
    finally:
        manager_inputs.pending.pop(key, None)
