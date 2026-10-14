from unittest.mock import Mock
import pytest
from app.config.settings import settings
from app.workers import customer_gmail_readonly_smoke as smoke


def test_readonly_smoke_only_calls_gmail_metadata(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "external_writes_enabled", False)
    monkeypatch.delenv("EMAIL_PRODUCTION_ACTIVATED", raising=False)
    calls = []
    class Provider:
        def readonly_smoke_check(self, max_messages):
            calls.append(("metadata", max_messages))
            return {"account": "hidden@private.invalid", "messages_found": 1}
    monkeypatch.setattr("app.integrations.email.secret_files.prepare_secret_files",
        lambda role: calls.append(("prepare", role)))
    monkeypatch.setattr("app.integrations.email.gmail_provider.GmailEmailProvider", Provider)
    assert smoke.verify_once() is True
    assert calls == [("prepare", "customer"), ("metadata", 1)]


@pytest.mark.parametrize("production,external,active", [
    (False, False, False), (True, True, False), (True, False, True)
])
def test_smoke_fails_closed(monkeypatch, production, external, active):
    monkeypatch.setattr(settings, "environment", "production" if production else "staging")
    monkeypatch.setattr(settings, "external_writes_enabled", external)
    monkeypatch.setenv("EMAIL_PRODUCTION_ACTIVATED", "true" if active else "false")
    with pytest.raises(RuntimeError):
        smoke.verify_once()
