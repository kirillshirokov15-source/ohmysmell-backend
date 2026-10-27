import pytest

from app.bot.manager_fast_cards import enabled


def test_fast_cards_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OMS_MANAGER_FAST_CARDS_ENABLED", raising=False)
    assert enabled() is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", " TRUE "])
def test_fast_cards_explicit_opt_in(monkeypatch, value):
    monkeypatch.setenv("OMS_MANAGER_FAST_CARDS_ENABLED", value)
    assert enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "off", "unexpected"])
def test_fast_cards_rollback(monkeypatch, value):
    monkeypatch.setenv("OMS_MANAGER_FAST_CARDS_ENABLED", value)
    assert enabled() is False
