"""Additive migration on verified staging; requires a passing fresh rehearsal."""
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine
from alembic import command
from alembic.config import Config
from app.bot.staging_runner import validate_staging_config, activate_staging_config


def main():
    load_dotenv()
    staging = validate_staging_config(dict(os.environ))
    rehearsal = json.loads(Path(".staging-artifacts/validation.json").read_text(encoding="utf-8"))
    if rehearsal.get("alembic_check") != "passed":
        raise RuntimeError("Fresh schema rehearsal must pass first")
    activate_staging_config(staging)
    engine = create_engine(staging.database_url, connect_args={"connect_timeout": 10})
    with engine.connect() as connection:
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()
        command.current(config)
        command.check(config)
    engine.dispose()
    print("STAGING upgrade and Alembic check passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("STAGING migration failed: " + type(error).__name__)
        raise SystemExit(1)
