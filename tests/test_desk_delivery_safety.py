import asyncio
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.config.settings import settings
from app.services.desk_delivery_policy import DeskDeliveryPolicy, DeskDeliveryConfigurationError
from app.bot.worker_errors import WorkerConfigurationError, startup_error_category


@pytest.fixture
def restricted(monkeypatch):
    monkeypatch.setattr(settings, "environment", "staging")
    for suffix in ("CLIENT_RECIPIENT_IDS", "MANAGER_CHAT_IDS", "MESSAGE_IDS", "DRAFT_IDS", "NOT_BEFORE"):
        monkeypatch.delenv("ORDER_DESK_STAGING_" + suffix, raising=False)


def test_staging_sending_flag_alone_grants_nothing(restricted, monkeypatch):
    monkeypatch.setenv("ORDER_DESK_SEND_ENABLED", "true")
    for role in ("manager", "client"):
        policy = DeskDeliveryPolicy.load(role)
        assert not policy.permits(SimpleNamespace(id=1,draft_id=1,destination=898019732))


def test_destination_and_explicit_scope_are_both_required(restricted, monkeypatch):
    monkeypatch.setenv("ORDER_DESK_STAGING_CLIENT_RECIPIENT_IDS", "898019732")
    monkeypatch.setenv("ORDER_DESK_STAGING_MESSAGE_IDS", "1")
    monkeypatch.setenv("ORDER_DESK_STAGING_DRAFT_IDS", "10")
    monkeypatch.setenv("ORDER_DESK_STAGING_NOT_BEFORE", "2026-10-09T00:00:00Z")
    policy = DeskDeliveryPolicy.load("client")
    assert policy.permits(SimpleNamespace(id=1,draft_id=None,destination=898019732))
    assert policy.permits(SimpleNamespace(id=2,draft_id=10,destination=898019732,created_at=datetime(2026,10,9,tzinfo=timezone.utc)))
    assert not policy.permits(SimpleNamespace(id=2,draft_id=None,destination=898019732))
    assert not policy.permits(SimpleNamespace(id=1,draft_id=10,destination=333))
    assert not DeskDeliveryPolicy.load("manager").permits(SimpleNamespace(id=1,draft_id=10,destination=-100123456))


@pytest.mark.parametrize("value", ["abc", "0", "-1", "1,", "9223372036854775808"])
def test_malformed_scope_fails_closed_without_echoing_input(restricted, monkeypatch, value):
    monkeypatch.setenv("ORDER_DESK_STAGING_MESSAGE_IDS", value)
    with pytest.raises(DeskDeliveryConfigurationError, match="Invalid staging"):
        DeskDeliveryPolicy.load("client")


def test_other_environments_keep_existing_delivery_contract(monkeypatch):
    monkeypatch.setattr(settings,"environment","development")
    assert DeskDeliveryPolicy.load("client").permits(SimpleNamespace(id=1,draft_id=None,destination=333))


@pytest.mark.parametrize("error,category", [
    (WorkerConfigurationError("client_bot_token_missing"),"client_bot_token_missing"),
    (WorkerConfigurationError("bot_roles_share_identity"),"bot_roles_share_identity"),
    (ValueError("DATABASE_URL=private-value"),"runtime_configuration_invalid"),
    (WorkerConfigurationError("private-value"),"runtime_configuration_invalid"),
    (DeskDeliveryConfigurationError("private-value"),"invalid_staging_delivery_scope"),
    (TimeoutError("private-value"),"dependency_unavailable"),
])
def test_startup_error_categories_are_safe(error, category):
    assert startup_error_category(error)==category


def test_startup_log_includes_reason_without_exception_body(monkeypatch, caplog):
    import app.bot.runtime as runtime
    monkeypatch.setattr(runtime,"run_worker",AsyncMock(side_effect=WorkerConfigurationError("client_worker_disabled", "secret-token")))
    monkeypatch.setattr(runtime,"configure_application_logging",lambda:None)
    monkeypatch.setattr(runtime.logger,"handlers",[caplog.handler])
    monkeypatch.setattr(runtime.logger,"propagate",False)
    with caplog.at_level(logging.ERROR),pytest.raises(SystemExit):runtime.main("client")
    record=json.loads(caplog.records[-1].message)
    assert record["reason"]=="client_worker_disabled" and record["role"]=="client"
    assert "secret-token" not in caplog.text


def test_worker_health_routes_and_readiness():
    from app.bot.runtime import worker_health_app
    from aiohttp.test_utils import make_mocked_request
    async def run():
        failure=asyncio.Event();state={"polling":False,"role":"client","external_writes":False}
        app=worker_health_app(state,failure)
        async def call(path):
            request=make_mocked_request("GET",path,app=app)
            match=await app.router.resolve(request)
            return await match.handler(request)
        assert (await call("/health")).status==200
        assert (await call("/ready")).status==503
        state["polling"]=True
        assert (await call("/ready")).status==200
        failure.set()
        assert (await call("/health")).status==503
        assert (await call("/ready")).status==503
    asyncio.run(run())
