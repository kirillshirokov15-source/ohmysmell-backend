import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


def test_manager_authorization_uses_async_session_repository():
    from app.repositories import manager_repository

    result = Mock()
    result.scalar_one_or_none.return_value = 7
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    with patch.object(manager_repository, "async_session", return_value=SessionContext()):
        allowed = asyncio.run(
            manager_repository.ManagerRepository().is_active_by_telegram_id(123)
        )

    assert allowed is True
    session.execute.assert_awaited_once()
    assert not hasattr(manager_repository, "psycopg2")
    assert not hasattr(manager_repository, "is_active_manager")


def test_unknown_or_inactive_manager_is_not_authorized():
    from app.repositories import manager_repository

    result = Mock()
    result.scalar_one_or_none.return_value = None
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    with patch.object(manager_repository, "async_session", return_value=SessionContext()):
        allowed = asyncio.run(
            manager_repository.ManagerRepository().is_active_by_telegram_id(404)
        )

    assert allowed is False
