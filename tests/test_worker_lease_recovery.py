"""The poller must fail closed on a lost PostgreSQL advisory-lock session."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot.runtime import lease_is_healthy, release_poller_lock


def test_poller_probe_commits_a_live_connection():
    conn = SimpleNamespace(scalar=AsyncMock(return_value=1), commit=AsyncMock())
    assert asyncio.run(lease_is_healthy(conn, timeout=0.2)) is True
    conn.scalar.assert_awaited_once()
    conn.commit.assert_awaited_once()


def test_failed_probe_does_not_reuse_failed_transaction():
    conn = SimpleNamespace(scalar=AsyncMock(side_effect=RuntimeError("broken connection")),
                           commit=AsyncMock(), execute=AsyncMock())
    assert asyncio.run(lease_is_healthy(conn, timeout=0.2)) is False
    conn.commit.assert_not_awaited()
    conn.execute.assert_not_awaited()


def test_lock_release_is_skipped_after_ownership_loss():
    conn = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    assert asyncio.run(release_poller_lock(conn, 19, ownership_lost=True)) is False
    conn.execute.assert_not_awaited()
    conn.commit.assert_not_awaited()


def test_graceful_release_sends_unlock_and_commits():
    conn = SimpleNamespace(execute=AsyncMock(return_value=None), commit=AsyncMock())
    assert asyncio.run(release_poller_lock(conn, 19, ownership_lost=False)) is True
    conn.execute.assert_awaited_once()
    conn.commit.assert_awaited_once()


def test_release_failure_does_not_mask_original_database_error():
    conn = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("broken transaction")),
                           commit=AsyncMock())
    assert asyncio.run(release_poller_lock(conn, 19, ownership_lost=False)) is False
    conn.commit.assert_not_awaited()
