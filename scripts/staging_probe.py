"""Read-only diagnostics. Never imports the application database engine."""
import os
import json
from dotenv import load_dotenv
import psycopg2
from app.bot.staging_runner import validate_staging_config


def main():
    load_dotenv()
    config = validate_staging_config(dict(os.environ))
    report = {"staging_config": "validated", "credentials_present": {
        key: bool(os.getenv(key)) for key in (
            "MOYSKLAD_TOKEN", "TELEGRAM_BOT_TOKEN", "INTERNAL_API_TOKEN",
            "GMAIL_CREDENTIALS_FILE", "GMAIL_TOKEN_FILE")}}
    try:
        with psycopg2.connect(config.database_url, connect_timeout=10) as conn:
            conn.set_session(readonly=True)
            with conn.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                report["revisions"] = [row[0] for row in cursor.fetchall()]
                for table in ("customers", "draft_orders", "orders"):
                    cursor.execute(f"SELECT count(*) FROM {table}")
                    report[table] = cursor.fetchone()[0]
                cursor.execute("SELECT has_database_privilege(current_user, current_database(), 'CREATE')")
                report["can_create_isolated_schema"] = cursor.fetchone()[0]
                cursor.execute("SELECT total, moysklad_order_id IS NULL FROM orders WHERE id = 1")
                original = cursor.fetchone()
                report["original_order_1"] = {"total_minor": original[0], "moysklad_id_null": original[1]} if original else None
                cursor.execute("SELECT count(*) FROM order_items WHERE qty <= 0 OR price < 0 OR item_total <> price * qty")
                report["invalid_order_items"] = cursor.fetchone()[0]
    except Exception as error:
        report["connection_error_type"] = type(error).__name__
    print(json.dumps(report))


if __name__ == "__main__":
    main()
