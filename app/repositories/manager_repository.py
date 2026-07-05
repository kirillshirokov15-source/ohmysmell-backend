import psycopg2
from app.config.settings import settings


def is_active_manager(telegram_id: int) -> bool:
    with psycopg2.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM managers
                    WHERE telegram_id = %s
                    AND is_active = true
                );
                """,
                (telegram_id,),
            )
            return cur.fetchone()[0]