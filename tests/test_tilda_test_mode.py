"""Safety gates for Tilda test orders on an existing published ST100 cart."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api import tilda as tilda_api

app = FastAPI()
app.include_router(tilda_api.router)
URL = "/integrations/tilda/orders"
SECRET = "t" * 32


@pytest.fixture(autouse=True)
def staging_test_env(monkeypatch):
    monkeypatch.setenv("TILDA_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TILDA_ALLOW_QUERY_SECRET", "false")
    monkeypatch.setenv("TILDA_ENABLED", "false")
    monkeypatch.setenv("TILDA_TEST_MODE", "true")
    monkeypatch.setenv("TILDA_TEST_EMAIL", "test@example.com")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    monkeypatch.delenv("TILDA_FIELD_MAP_JSON", raising=False)


def post(payload, secret=SECRET):
    with TestClient(app) as client:
        return client.post(URL, data=payload, headers={"X-Tilda-Secret": secret})


def test_probe_still_accepted():
    result = post({"test": "test"})
    assert result.status_code == 200 and result.text == "ok"


def test_wrong_secret_rejected():
    result = post({"Email": "test@example.com"}, "wrong")
    assert result.status_code == 401


@pytest.mark.parametrize("payload", [
    {"Email": "other@example.com"},
    {"Email": ""},
    {"Name": "Someone"},
    {"Email": "another@example.org"},
])
def test_other_or_missing_email_ignored_without_normalizing(payload, monkeypatch):
    def must_not_normalize(*args, **kwargs):
        raise AssertionError("Non-test payload reached normalization")
    monkeypatch.setattr(tilda_api, "normalize", must_not_normalize)
    result = post(payload)
    assert result.status_code == 200 and result.text == "ignored"


def test_exact_test_email_proceeds_to_normalization_and_intake(monkeypatch):
    captured = []
    def fake_normalize(data, key):
        captured.append(data)
        return {"external_id": "test-order-id"}
    class FakeIntake:
        async def submit(self, payload):
            assert payload["external_id"] == "test-order-id"
            return {"draft_id": 123, "duplicate": False, "status": "needs_review"}
    monkeypatch.setattr(tilda_api, "normalize", fake_normalize)
    monkeypatch.setattr(tilda_api, "TildaIntake", FakeIntake)
    result = post({"Email": " TEST@example.com "})
    assert result.status_code == 202
    assert result.json()["draft_id"] == 123
    assert captured == [{"Email": " TEST@example.com "}]


@pytest.mark.parametrize("variable,value", [
    ("TILDA_ENABLED", "true"),
    ("RAILWAY_ENVIRONMENT_NAME", "production"),
    ("TILDA_TEST_EMAIL", ""),
    ("TILDA_TEST_EMAIL", "not-an-email"),
])
def test_invalid_test_mode_fails_closed(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    assert post({"Email": "other@example.com"}).status_code == 503


def test_normal_orders_disabled_when_no_test_mode(monkeypatch):
    monkeypatch.setenv("TILDA_TEST_MODE", "false")
    assert post({"Email": "test@example.com"}).status_code == 503
