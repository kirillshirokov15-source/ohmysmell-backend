"""Read-only schema/model check against the explicitly selected staging DB."""
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from alembic import command
from alembic.config import Config
from app.bot.staging_runner import validate_staging_config, activate_staging_config


def main():
    load_dotenv()
    staging = validate_staging_config(dict(os.environ))
    activate_staging_config(staging)
    engine = create_engine(staging.database_url, connect_args={"connect_timeout": 10, "options": "-c default_transaction_read_only=on"})
    try:
        with engine.connect() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.current(config)
            command.check(config)
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Staging schema check failed:", type(error).__name__)
        raise SystemExit(1)
