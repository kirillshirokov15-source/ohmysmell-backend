"""Safety checks for the production-owned X release gate."""
from datetime import datetime, timezone, timedelta

import pytest

from app.config.settings import settings
from app.workers.hatuna_auto import activation_time, require_live_gates


def test_live_requires_explicit_accounting_policy_and_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "external_writes_enabled", True)
    monkeypatch.setenv("OMS_HATUNA_AUTO_WRITES_ENABLED", "true")
    monkeypatch.setenv("OMS_OWN_X_LIVE_ENABLED", "true")
    monkeypatch.setenv("OMS_HATUNA_AUTO_NOT_BEFORE", "2026-10-25T03:00:00+02:00")
    monkeypatch.delenv("OMS_X_COST_POLICY", raising=False)
    with pytest.raises(RuntimeError, match="policy"):
        require_live_gates()
    monkeypatch.setenv("OMS_X_COST_POLICY", "provisional_minus_1000")
    assert require_live_gates() == datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)


def test_live_rejects_incomplete_or_timezone_naive_activation(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    for invalid in ("", "tomorrow", "2026-10-25T03:00:00"):
        monkeypatch.setenv("OMS_HATUNA_AUTO_NOT_BEFORE", invalid)
        with pytest.raises(RuntimeError):
            activation_time()


def test_live_rejects_unapproved_inventory_write_flags(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "external_writes_enabled", True)
    monkeypatch.setenv("OMS_X_COST_POLICY", "provisional_minus_1000")
    monkeypatch.setenv("OMS_HATUNA_AUTO_NOT_BEFORE", "2026-10-25T03:00:00+02:00")
    monkeypatch.setenv("OMS_HATUNA_AUTO_WRITES_ENABLED", "false")
    monkeypatch.setenv("OMS_OWN_X_LIVE_ENABLED", "true")
    with pytest.raises(RuntimeError, match="OMS_HATUNA_AUTO_WRITES_ENABLED"):
        require_live_gates()
    monkeypatch.setenv("OMS_HATUNA_AUTO_WRITES_ENABLED", "true")
    monkeypatch.setenv("OMS_OWN_X_LIVE_ENABLED", "false")
    with pytest.raises(RuntimeError, match="OMS_OWN_X_LIVE_ENABLED"):
        require_live_gates()


def test_staging_still_can_run_fake_provider_without_live_switches(monkeypatch):
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "external_writes_enabled", False)
    monkeypatch.delenv("OMS_X_COST_POLICY", raising=False)
    assert require_live_gates() is None
    assert activation_time() is None
