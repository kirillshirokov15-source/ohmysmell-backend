import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.logging_utils import configure_application_logging, log_event


def test_application_metrics_reach_stdout_once_without_dependency_debug(monkeypatch, capsys):
    logger = logging.getLogger("app")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logging.NOTSET)
    monkeypatch.setattr(logger, "propagate", True)
    dependency = logging.getLogger("sqlalchemy.engine")
    old_level = dependency.level
    configure_application_logging()
    configure_application_logging()
    log_event(logging.getLogger("app.integrations.moysklad"),
              "moysklad_catalog_cache_hit", operation="get_products")
    output = capsys.readouterr().out
    assert output.count("moysklad_catalog_cache_hit") == 1
    assert dependency.level == old_level


@pytest.mark.parametrize("failed", [False, True])
def test_database_health_records_latency_without_connection_error_details(monkeypatch, caplog, failed):
    from app.config.settings import settings
    from app.database import connection, session

    monkeypatch.setattr(settings, "database_url", "postgresql://unit-test")
    context = AsyncMock()
    context.__aenter__.return_value = SimpleNamespace(execute=AsyncMock())
    if failed:
        context.__aenter__.side_effect = RuntimeError("private credential must never be logged")
    monkeypatch.setattr(session, "engine", SimpleNamespace(connect=Mock(return_value=context)))
    with caplog.at_level(logging.INFO, logger=connection.__name__):
        assert asyncio.run(connection.check_database_connection()) is not failed
    event = json.loads(next(r.message for r in caplog.records if "database_health_completed" in r.message))
    assert event["connected"] is not failed
    assert event["duration_ms"] >= 0
    assert event.get("error_type") == ("RuntimeError" if failed else None)
    assert "private credential" not in caplog.text
