import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from app.config.settings import settings


@pytest.mark.parametrize("role", ["client", "manager", "email"])
def test_worker_config_does_not_require_backend_secrets(monkeypatch, role):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "internal_api_token", "")
    monkeypatch.setattr(settings, "cors_origins", [])
    monkeypatch.setattr(settings, "debug_endpoints_enabled", False)
    monkeypatch.setattr(settings, "external_writes_enabled", False)
    settings.validate_runtime(role)
    with pytest.raises(ValueError):
        settings.validate_runtime("backend")


@pytest.mark.parametrize("role", ["manager", "client"])
def test_worker_missing_only_own_token_fails(monkeypatch, role):
    from app.bot.runtime import validate_worker
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.delenv("CLIENT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="TOKEN"):
        validate_worker(role)


def test_email_missing_credentials_fails_before_network(monkeypatch):
    from app.workers.email_runtime import validate_email
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "gmail_token_file", "")
    with pytest.raises(ValueError, match="GMAIL_TOKEN_FILE"):
        validate_email()


def test_monitoring_default_never_imports_sdk(monkeypatch):
    import sys
    from app.monitoring import configure_monitoring
    monkeypatch.delenv("MONITORING_ENABLED", raising=False)
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)
    configure_monitoring()


def test_monitoring_removes_all_pii_and_secrets():
    from app.monitoring import scrub_event
    event = {"request": {"body": "SECRET"}, "user": {"email": "SECRET"}, "message": "SECRET",
        "breadcrumbs": ["SECRET"], "extra": {"token": "SECRET"},
        "exception": {"values": [{"type": "ValueError", "value": "SECRET",
            "stacktrace": {"frames": [{"filename": "app/x.py", "lineno": 1, "vars": {"token": "SECRET"}, "context_line": "SECRET"}]}}]}}
    clean = scrub_event(event, {})
    assert "SECRET" not in json.dumps(clean)
    assert clean["exception"]["values"][0]["type"] == "ValueError"


def test_http_retries_only_safe_reads():
    from app.integrations.http_tls import verified_session
    with verified_session() as session:
        retry = session.get_adapter("https://example.invalid").max_retries
        assert retry.total == 2 and retry.backoff_factor > 0
        assert retry.allowed_methods == {"GET", "HEAD"}
        for status in (401, 400):
            assert not retry.is_retry("GET", status)
        for status in (429, 502, 503, 504):
            assert retry.is_retry("GET", status)
            assert not retry.is_retry("POST", status)


@pytest.mark.parametrize("status", [401, 429, 500])
def test_provider_http_failure_never_exposes_response_to_api(monkeypatch, status, caplog):
    from tests.asgi_client import request
    import app.main as main
    from requests import HTTPError, Response
    response = Response(); response.status_code = status
    response._content = b'SECRET provider body'
    monkeypatch.setattr(settings, "internal_api_token", "test-token")
    monkeypatch.setattr(main, "get_order", AsyncMock(side_effect=HTTPError("SECRET", response=response)))
    code, body, _ = asyncio.run(request(main.app, "GET", "/orders/1", headers={"X-Internal-API-Token": "test-token"}))
    assert code == 503 and body["detail"]["code"] == "service_unavailable"
    assert "SECRET" not in json.dumps(body) + caplog.text


@pytest.mark.parametrize("identifier", ["0", "-1", "9999999999999999999999999", "1%20OR%201=1"])
def test_api_unsafe_ids_rejected_before_db(monkeypatch, identifier):
    from tests.asgi_client import request
    import app.main as main
    monkeypatch.setattr(settings, "internal_api_token", "test-token")
    read = AsyncMock(); monkeypatch.setattr(main, "get_order", read)
    code, _, _ = asyncio.run(request(main.app, "GET", "/orders/" + identifier, headers={"X-Internal-API-Token": "test-token"}))
    assert code == 422
    read.assert_not_awaited()


def test_health_live_does_not_fetch_dependencies_and_ready_fails_db(monkeypatch):
    from tests.asgi_client import request
    import app.main as main
    db = AsyncMock(return_value=False); monkeypatch.setattr(main, "check_database_connection", db)
    code, _, _ = asyncio.run(request(main.app, "GET", "/health"))
    assert code == 200; db.assert_not_awaited()
    code, _, _ = asyncio.run(request(main.app, "GET", "/ready"))
    assert code == 503


def test_client_real_dispatch_contract_and_role_boundary():
    from aiogram import Bot
    from aiogram.types import Update
    from aiogram.methods import SendMessage, AnswerCallbackQuery
    from tests.telegram_transport import TelegramSession
    from app.bot.client_bot import create_dispatcher
    channel = SimpleNamespace(handle=AsyncMock(return_value="Цена по запросу"))
    transport = TelegramSession()
    bot = Bot("123456:FAKE_LOCAL_TEST_TOKEN", session=transport)
    async def run():
        dp = create_dispatcher(channel)
        message = Update.model_validate({"update_id": 1, "message": {"message_id": 1, "date": 1,
            "chat": {"id": 42, "type": "private"}, "from": {"id": 42, "is_bot": False, "first_name": "Тест"}, "text": "/start"}})
        await dp.feed_update(bot, message)
        callback = Update.model_validate({"update_id": 2, "callback_query": {"id": "cb", "chat_instance": "fake",
            "from": {"id": 42, "is_bot": False, "first_name": "Тест"}, "data": "order:paid:1:0"}})
        await dp.feed_update(bot, callback)
        await bot.session.close()
    asyncio.run(run())
    channel.handle.assert_awaited_once_with(42, 1, "/start", name="Тест", verified_phone=None)
    assert isinstance(transport.calls[0], SendMessage)
    assert isinstance(transport.calls[1], AnswerCallbackQuery)
    assert transport.closed


@pytest.mark.parametrize("failure", ["timeout", "bad_request"])
def test_client_telegram_send_error_has_controlled_recovery(failure):
    from aiogram import Bot
    from aiogram.types import Update
    from aiogram.methods import SendMessage
    from aiogram.exceptions import TelegramNetworkError, TelegramBadRequest
    from tests.telegram_transport import TelegramSession
    from app.bot.client_bot import create_dispatcher
    method = SendMessage(chat_id=42, text="x")
    error = (TelegramNetworkError if failure == "timeout" else TelegramBadRequest)(method=method, message="SECRET")
    transport = TelegramSession(error)
    bot = Bot("123456:FAKE_LOCAL_TEST_TOKEN", session=transport)
    service = SimpleNamespace(handle=AsyncMock(return_value="Заявка №1"))
    update = Update.model_validate({"update_id": 1, "message": {"message_id": 1, "date": 1,
        "chat": {"id": 42, "type": "private"}, "from": {"id": 42, "is_bot": False, "first_name": "Тест"}, "text": "/send"}})
    asyncio.run(create_dispatcher(service).feed_update(bot, update))
    assert service.handle.await_count == 1
    assert "временно недоступен" in transport.calls[1].text and "SECRET" not in transport.calls[1].text


def test_email_crash_after_commit_before_cursor_replays_safely():
    from tests.test_staging_readiness import RepeatingProvider, worker_message, MemoryCursorRepository, IdempotentPipeline
    from app.workers.email_ingestion import EmailIngestionWorker
    provider = RepeatingProvider([worker_message("crash:1")])
    pipeline, cursor = IdempotentPipeline(), MemoryCursorRepository()
    original = cursor.set
    cursor.set = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(EmailIngestionWorker(provider, pipeline, cursor).run_once())
    assert pipeline.created == 1 and cursor.value is None
    cursor.set = original
    asyncio.run(EmailIngestionWorker(provider, pipeline, cursor).run_once())
    assert pipeline.created == 1 and cursor.value == "history-2"


def test_email_error_log_cannot_inject_lines(caplog):
    from tests.test_staging_readiness import RepeatingProvider, worker_message, MemoryCursorRepository, IdempotentPipeline
    from app.workers.email_ingestion import EmailIngestionWorker
    identifier = 'bad"\nFORGED'
    with caplog.at_level(logging.INFO):
        asyncio.run(EmailIngestionWorker(RepeatingProvider([worker_message(identifier)]),
            IdempotentPipeline(identifier), MemoryCursorRepository()).run_once())
    for record in caplog.records:
        if record.name == "app.workers.email_ingestion":
            assert "\n" not in record.message
            json.loads(record.message)

@pytest.mark.parametrize("ownership_lost", [False, True])
def test_client_runtime_shutdown_closes_session_and_releases_engine(monkeypatch, ownership_lost):
    import app.bot.runtime as runtime
    import app.bot.client_bot as client
    import app.database.session as database
    from app.workers import lifecycle
    from aiohttp import web
    monkeypatch.setattr(runtime, "validate_worker", lambda role: "123456:FAKE_LOCAL_TEST_TOKEN")
    monkeypatch.setattr(lifecycle, "install_stop_signals", lambda loop, stop: lambda: None)
    connection = AsyncMock()
    connection.scalar.side_effect = [True, RuntimeError("ownership lost")] if ownership_lost else None
    connection.scalar.return_value = True
    context = AsyncMock(); context.__aenter__.return_value = connection
    engine = SimpleNamespace(connect=Mock(return_value=context), dispose=AsyncMock())
    monkeypatch.setattr(database, "engine", engine)
    bot = SimpleNamespace(id=123456, get_webhook_info=AsyncMock(return_value=SimpleNamespace(url="")),
        get_me=AsyncMock(), session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(runtime, "OwnedBot", lambda token, failure: bot)
    async def run():
        done = asyncio.Event()
        async def poll(*args, **kwargs):
            if ownership_lost:
                await done.wait()
        async def stop(): done.set()
        dispatcher = SimpleNamespace(start_polling=poll, stop_polling=stop)
        monkeypatch.setattr(client, "create_dispatcher", lambda: dispatcher)
        monkeypatch.setattr(web, "AppRunner", lambda server: SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock()))
        monkeypatch.setattr(web, "TCPSite", lambda *a: SimpleNamespace(start=AsyncMock()))
        if ownership_lost:
            with pytest.raises(RuntimeError, match="ownership"):
                await runtime.run_worker("client")
        else:
            await runtime.run_worker("client")
    asyncio.run(run())
    engine.dispose.assert_awaited_once()
    bot.session.close.assert_awaited_once()
    connection.__class__


def test_email_runtime_poll_signal_and_cleanup(monkeypatch):
    from app.workers import email_runtime as runtime
    import app.database.session as database
    from aiohttp import web
    monkeypatch.setattr(runtime, "validate_email", lambda: None)
    stopper = []
    monkeypatch.setattr(runtime, "install_stop_signals", lambda loop, stop: stopper.append(stop) or (lambda: None))
    context = AsyncMock(); connection = AsyncMock(); context.__aenter__.return_value = connection
    connection.scalar.return_value = True
    engine = SimpleNamespace(connect=Mock(return_value=context), dispose=AsyncMock())
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(web, "AppRunner", lambda server: SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock()))
    monkeypatch.setattr(web, "TCPSite", lambda *a: SimpleNamespace(start=AsyncMock()))
    async def poll():
        stopper[0].set()
        return {"received": 1, "processed": 1, "failed": 0}
    worker = SimpleNamespace(run_once=AsyncMock(side_effect=poll))
    asyncio.run(runtime.run(worker))
    worker.run_once.assert_awaited_once(); engine.dispose.assert_awaited_once()


def test_optional_sentry_adapter_contract_has_no_default_telemetry(monkeypatch):
    sdk = pytest.importorskip("sentry_sdk")
    from app.monitoring import configure_monitoring
    init = Mock(); monkeypatch.setattr(sdk, "init", init)
    monkeypatch.setenv("MONITORING_ENABLED", "true")
    monkeypatch.setenv("SENTRY_DSN", "https://fake@example.invalid/1")
    configure_monitoring()
    config = init.call_args.kwargs
    assert config["default_integrations"] is False
    assert config["include_local_variables"] is False
    assert config["send_default_pii"] is False and config["traces_sample_rate"] == 0


@pytest.mark.parametrize("run,schema", [("public", "public"), ("a"*32, "public"), ("a"*32, "oms_fixture_"+"b"*32)])
def test_synthetic_cleanup_refuses_arbitrary_targets(run, schema):
    from scripts.synthetic_data import validate_manifest
    with pytest.raises(ValueError):
        validate_manifest({"run": run, "schema": schema, "database": "db"}, run, "db")


def test_expired_gmail_history_bootstraps_without_cursor_loss(monkeypatch):
    from app.integrations.email.gmail_provider import GmailEmailProvider
    provider = GmailEmailProvider(service=Mock())
    error = RuntimeError("expired history")
    error.resp = SimpleNamespace(status=404)
    monkeypatch.setattr(provider, "_message_ids_from_history", Mock(side_effect=error))
    bootstrap = Mock(return_value=(["message-1"], "new-history"))
    monkeypatch.setattr(provider, "_bootstrap", bootstrap)
    monkeypatch.setattr(provider, "_get_message", lambda *_: "decoded-message")
    batch = provider._fetch_sync("old-history")
    assert batch.next_cursor == "new-history" and batch.messages == ["decoded-message"]
    bootstrap.assert_called_once()


@pytest.mark.parametrize("failure", ["timeout", "unauthorized", "rate_limit", "server"])
def test_moysklad_gateway_failure_latency_no_error_body(failure, caplog):
    from app.integrations.moysklad.async_gateway import AsyncMoySkladGateway, ProductCatalogTTLCache
    from requests import Timeout, HTTPError
    error = Timeout("PRIVATE") if failure == "timeout" else HTTPError("PRIVATE")
    client = Mock(); client.get_products.side_effect = error
    gateway = AsyncMoySkladGateway(client, ProductCatalogTTLCache())
    with caplog.at_level(logging.ERROR), pytest.raises(type(error)):
        asyncio.run(gateway.get_products())
    assert "PRIVATE" not in caplog.text
    records = [r for r in caplog.records if "moysklad_call_completed" in r.message]
    assert records and json.loads(records[-1].message)["duration_ms"] >= 0
