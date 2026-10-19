"""Real PostgreSQL locking with a fake Telegram transport; no external sends."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from tests.test_postgres_staging import staging_sessions
from app.services import telegram_cards as cards

pytestmark = pytest.mark.staging


def test_card_claim_survives_concurrent_worker_and_restart(staging_sessions, monkeypatch):
    factory, engine = staging_sessions
    monkeypatch.setattr(cards, "async_session", factory)
    async def run():
        entity = "order:" + uuid4().hex[:20]
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(message_id=101)
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=send), delete_message=AsyncMock())
        task = asyncio.create_task(cards.publish(bot, -999, entity, "synthetic"))
        await entered.wait()
        try:
            with pytest.raises(cards.CardDeliveryUncertain):
                await cards.publish(bot, -999, entity, "synthetic")
        finally:
            release.set()
        await task
        await engine.dispose()
        # Reopen all connections; there is no in-memory delivery receipt.
        await cards.publish(bot, -999, entity, "synthetic")
        bot.send_message.assert_awaited_once()
        bot.send_message.side_effect = TimeoutError()
        with pytest.raises(TimeoutError):
            await cards.publish(bot, -999, entity, "changed")
        await engine.dispose()
        with pytest.raises(cards.CardDeliveryUncertain):
            await cards.publish(bot, -999, entity, "changed")
        await engine.dispose()
    asyncio.run(run())
