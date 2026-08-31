"""Explicit staging-only Telegram bot launcher.

Database-bound application modules are deliberately imported only after the
staging URL passes all safety checks and becomes the process-wide setting.
"""

from dataclasses import dataclass
import os

from dotenv import load_dotenv
import psycopg2
from sqlalchemy.engine import make_url

from app.config.settings import settings


STAGING_MANAGER_TELEGRAM_ID = 898019732
STAGING_DRAFT_ID = 2


@dataclass(frozen=True)
class StagingBotConfig:
    database_url: str
    database_host: str


def validate_staging_config(environment: dict[str, str]) -> StagingBotConfig:
    staging_raw = environment.get("STAGING_DATABASE_URL", "").strip()
    if not staging_raw:
        raise RuntimeError("STAGING_DATABASE_URL is required")

    production_raw = environment.get("DATABASE_URL", "").strip()
    if not production_raw:
        raise RuntimeError(
            "DATABASE_URL is required only for staging/production comparison"
        )

    if environment.get("EXTERNAL_WRITES_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise RuntimeError("External writes must be disabled for staging bot")

    production = make_url(production_raw)
    staging = make_url(staging_raw)
    production_identity = (
        production.host,
        production.database,
        production.username,
    )
    staging_identity = (staging.host, staging.database, staging.username)
    if staging_identity == production_identity or (
        staging.host,
        staging.database,
    ) == (production.host, production.database):
        raise RuntimeError("Staging database must differ from production")
    if not all(staging_identity):
        raise RuntimeError("Staging database host, database, and user are required")

    return StagingBotConfig(
        database_url=staging_raw,
        database_host=staging.host,
    )


def activate_staging_config(config: StagingBotConfig) -> None:
    # app.database.session is imported later and binds its engine exactly once
    # to this selected URL. DATABASE_URL itself is intentionally not changed.
    settings.database_url = config.database_url
    settings.external_writes_enabled = False


def read_startup_diagnostics(database_url: str) -> tuple[bool, bool]:
    with psycopg2.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    EXISTS(
                        SELECT 1 FROM managers
                        WHERE telegram_id = %s AND is_active = true
                    ),
                    EXISTS(SELECT 1 FROM draft_orders WHERE id = %s)
                """,
                (STAGING_MANAGER_TELEGRAM_ID, STAGING_DRAFT_ID),
            )
            manager_exists, draft_exists = cursor.fetchone()
    return bool(manager_exists), bool(draft_exists)


def startup_lines(
    config: StagingBotConfig,
    manager_exists: bool,
    draft_exists: bool,
) -> list[str]:
    return [
        "Environment: STAGING",
        f"Database host: {config.database_host}",
        "External writes: DISABLED",
        (
            f"Active manager {STAGING_MANAGER_TELEGRAM_ID} exists: "
            f"{'yes' if manager_exists else 'no'}"
        ),
        f"Draft #{STAGING_DRAFT_ID} exists: {'yes' if draft_exists else 'no'}",
    ]


def main() -> None:
    load_dotenv(".env")
    config = validate_staging_config(dict(os.environ))
    activate_staging_config(config)
    manager_exists, draft_exists = read_startup_diagnostics(config.database_url)
    for line in startup_lines(config, manager_exists, draft_exists):
        print(line)

    # These imports must remain after activate_staging_config: repositories and
    # async sessions then bind only to the validated staging database.
    launch_staging_bot()


def launch_staging_bot() -> None:
    from app.bot.telegram_bot import run_bot

    run_bot(drop_pending_updates=True)


if __name__ == "__main__":
    main()
