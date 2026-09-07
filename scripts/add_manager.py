"""Explicit staging-only manager administration."""
import argparse
import os
import psycopg2
from dotenv import load_dotenv
from app.bot.staging_runner import validate_staging_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram-id", type=int, required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    if args.telegram_id <= 0 or not 1 <= len(args.name) <= 255:
        parser.error("Invalid manager identity")
    load_dotenv()
    config = validate_staging_config(dict(os.environ))
    with psycopg2.connect(config.database_url, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO managers (telegram_id, name, is_active, created_at)
                VALUES (%s, %s, true, NOW()) ON CONFLICT (telegram_id)
                DO UPDATE SET name = EXCLUDED.name, is_active = true
            """, (args.telegram_id, args.name))
    print("STAGING manager saved")


if __name__ == "__main__":
    main()
