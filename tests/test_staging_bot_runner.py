import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.bot.staging_runner import (
    StagingBotConfig,
    activate_staging_config,
    launch_staging_bot,
    startup_lines,
    validate_staging_config,
)
from app.config.settings import settings


PRODUCTION_URL = "postgresql://prod_user:prod_password@prod.invalid/prod_db"
STAGING_URL = "postgresql://stage_user:stage_password@stage.invalid/stage_db"


def environment(**overrides):
    values = {
        "DATABASE_URL": PRODUCTION_URL,
        "STAGING_DATABASE_URL": STAGING_URL,
        "EXTERNAL_WRITES_ENABLED": "false",
    }
    values.update(overrides)
    return values


def test_staging_runner_requires_staging_url_and_never_falls_back():
    with pytest.raises(RuntimeError, match="STAGING_DATABASE_URL is required"):
        validate_staging_config(environment(STAGING_DATABASE_URL=""))


def test_staging_runner_blocks_external_writes():
    with pytest.raises(RuntimeError, match="External writes must be disabled"):
        validate_staging_config(environment(EXTERNAL_WRITES_ENABLED="true"))


def test_staging_and_production_database_cannot_be_identical():
    with pytest.raises(RuntimeError, match="must differ"):
        validate_staging_config(
            environment(STAGING_DATABASE_URL=PRODUCTION_URL)
        )


def test_same_host_and_database_are_blocked_even_for_another_user():
    with pytest.raises(RuntimeError, match="must differ"):
        validate_staging_config(environment(
            STAGING_DATABASE_URL=(
                "postgresql://another:password@prod.invalid/prod_db"
            )
        ))


def test_startup_diagnostics_do_not_expose_secrets():
    config = validate_staging_config(environment())
    output = "\n".join(startup_lines(config, True, True))
    assert "stage.invalid" in output
    assert "stage_password" not in output
    assert STAGING_URL not in output
    assert "TELEGRAM_BOT_TOKEN" not in output


def test_activation_selects_staging_before_async_manager_auth_import():
    old_database_url = settings.database_url
    old_external_writes = settings.external_writes_enabled
    try:
        activate_staging_config(StagingBotConfig(STAGING_URL, "stage.invalid"))
        assert settings.database_url == STAGING_URL
        assert settings.external_writes_enabled is False
    finally:
        settings.database_url = old_database_url
        settings.external_writes_enabled = old_external_writes


def test_telegram_draft_callback_router_is_registered():
    from app.bot import telegram_bot

    callbacks = {
        item.callback for item in telegram_bot.dp.callback_query.handlers
    }
    assert telegram_bot.draft_callback_handler in callbacks


def test_russian_labels_preserve_technical_callback_data():
    from types import SimpleNamespace

    from app.services.draft_telegram_service import build_draft_keyboard

    draft = SimpleNamespace(
        source="manual",
        id=2,
        status="needs_review",
        counterparty_id=None,
        counterparty_candidates=[],
        items=[],
    )
    buttons = [
        button
        for row in build_draft_keyboard(draft)["inline_keyboard"]
        for button in row
    ]
    by_label = {button["text"]: button["callback_data"] for button in buttons}
    assert by_label["Подтвердить: опт"] == "draft:type:wholesale:2"
    assert by_label["Подтвердить: розница"] == "draft:type:retail:2"
    assert by_label["Отклонить"] == "draft:reject:2"


def test_staging_runner_requests_pending_update_drop():
    from app.bot import telegram_bot

    with patch.object(telegram_bot, "run_bot") as run:
        launch_staging_bot()
    run.assert_called_once_with(drop_pending_updates=True)


def test_legacy_entrypoint_delegates_to_singleton_worker():
    from app.bot import telegram_bot
    with patch("app.bot.runtime.run_worker", new_callable=AsyncMock) as run:
        asyncio.run(telegram_bot.main(drop_pending_updates=True))
    run.assert_awaited_once_with("manager", drop_pending_updates=True)
