"""Native Tilda header authentication and side-effect-free connection probes."""
import asyncio
import json
import logging
from urllib.parse import urlencode
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from app.api import tilda
from app.api.safety import RequestSafetyMiddleware
from tests.asgi_client import request
from tests.test_tilda_orders import payload, tilda_config

SECRET = "synthetic-tilda-secret-32-characters-only"
URL = "/integrations/tilda/orders"


@pytest.fixture
def webhook(tilda_config, monkeypatch):
    submit = AsyncMock(return_value={"draft_id": 1, "duplicate": False})
    monkeypatch.setattr(tilda.TildaIntake, "submit", submit)
    app = FastAPI()
    app.include_router(tilda.router)
    app.add_middleware(RequestSafetyMiddleware)
    return app, submit


@pytest.mark.parametrize("header", ["X-Tilda-Secret", "x-tilda-secret", "X-TILDA-SECRET"])
@pytest.mark.parametrize("form", [True, False])
def test_native_header_order_authentication(webhook, header, form):
    app, submit = webhook
    data = payload()
    headers = {header: SECRET}
    if form:
        data["products"] = json.dumps(data["products"])
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        kwargs = {"content": urlencode(data).encode()}
    else:
        kwargs = {"payload": data}
    assert asyncio.run(request(app, "POST", URL, headers=headers, **kwargs))[0] == 200
    submit.assert_awaited_once()
    assert submit.await_args.args[0]["reported_total"] == 3030


@pytest.mark.parametrize("enabled", ["true", "false"])
@pytest.mark.parametrize("credentials", ["wrong", "missing", "body_only", "bearer_only"])
def test_handshake_requires_the_secret_even_when_intake_disabled(webhook, monkeypatch, enabled, credentials):
    app, submit = webhook
    monkeypatch.setenv("TILDA_ENABLED", enabled)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    body = b"test=test"
    if credentials == "wrong":
        headers["X-Tilda-Secret"] = "wrong-key"
    elif credentials == "body_only":
        body += ("&X-Tilda-Secret=" + SECRET).encode()
    elif credentials == "bearer_only":
        headers["Authorization"] = "Bearer " + SECRET
    assert asyncio.run(request(app, "POST", URL, headers=headers, content=body))[0] == 401
    submit.assert_not_awaited()


@pytest.mark.parametrize("enabled", ["true", "false", None])
@pytest.mark.parametrize("form", [True, False])
def test_authenticated_probe_is_repeatable_without_order_side_effects(webhook, monkeypatch, enabled, form, caplog):
    app, submit = webhook
    if enabled is None:
        monkeypatch.delenv("TILDA_ENABLED")
    else:
        monkeypatch.setenv("TILDA_ENABLED", enabled)
    # A connection test does not depend on order mappings, normalization or DB.
    monkeypatch.setenv("TILDA_FIELD_MAP_JSON", "not-configured-yet")
    def no_normalization(*args):
        raise AssertionError("Handshake must not enter order normalization")
    monkeypatch.setattr(tilda, "normalize", no_normalization)
    monkeypatch.setattr(tilda.logger, "handlers", [caplog.handler])
    monkeypatch.setattr(tilda.logger, "propagate", False)
    headers = {"X-Tilda-Secret": SECRET}
    if form:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        kwargs = {"content": b"test=test"}
    else:
        kwargs = {"payload": {"test": "test"}}
    with caplog.at_level(logging.INFO):
        for _ in range(2):
            code, body, response_headers = asyncio.run(request(app, "POST", URL, headers=headers, **kwargs))
            assert (code, body) == (200, "ok")
            assert response_headers[b"cache-control"] == b"no-store"
    submit.assert_not_awaited()
    assert "tilda_webhook_handshake_accepted" in caplog.text
    assert SECRET not in caplog.text


@pytest.mark.parametrize("secret", [None, "short"])
def test_probe_fails_closed_without_strong_server_secret(webhook, monkeypatch, secret):
    app, submit = webhook
    if secret is None:
        monkeypatch.delenv("TILDA_WEBHOOK_SECRET")
    else:
        monkeypatch.setenv("TILDA_WEBHOOK_SECRET", secret)
    assert asyncio.run(request(app, "POST", URL, {"test": "test"}, headers={"X-Tilda-Secret": secret or SECRET}))[0] == 503
    submit.assert_not_awaited()


@pytest.mark.parametrize("body", [b"test=other", b"test=test&test=test", b"test=test&tranid=other", b"test[test]=test"])
def test_only_exact_probe_shape_bypasses_order_normalization(webhook, body):
    app, submit = webhook
    assert asyncio.run(request(app, "POST", URL, content=body, headers={
        "X-Tilda-Secret": SECRET, "Content-Type": "application/x-www-form-urlencoded"}))[0] == 422
    submit.assert_not_awaited()


@pytest.mark.parametrize("enabled,expected", [("true", 200), ("false", 503)])
def test_real_order_with_test_field_is_never_silently_discarded(webhook, monkeypatch, enabled, expected):
    app, submit = webhook
    monkeypatch.setenv("TILDA_ENABLED", enabled)
    data = {**payload(), "test": "test"}
    assert asyncio.run(request(app, "POST", URL, data, headers={"X-Tilda-Secret": SECRET}))[0] == expected
    assert submit.await_count == (1 if enabled == "true" else 0)


@pytest.mark.parametrize("query_enabled,header,expected", [(False, None, 401), (True, None, 200), (True, "wrong", 401)])
def test_legacy_query_secret_remains_opt_in_and_cannot_override_header(webhook, monkeypatch, query_enabled, header, expected):
    app, submit = webhook
    monkeypatch.setenv("TILDA_ALLOW_QUERY_SECRET", str(query_enabled).lower())
    headers = {"X-Tilda-Secret": header} if header is not None else {}
    assert asyncio.run(request(app, "POST", URL + "?secret=" + SECRET, {"test": "test"}, headers=headers))[0] == expected
    submit.assert_not_awaited()
