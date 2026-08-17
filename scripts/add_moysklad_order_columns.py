import psycopg2

from app.config.settings import settings


def main():
    conn = psycopg2.connect(settings.database_url)

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                ALTER TABLE orders
                ADD COLUMN IF NOT EXISTS moysklad_order_id VARCHAR(255);

                ALTER TABLE orders
                ADD COLUMN IF NOT EXISTS moysklad_order_name VARCHAR(255);
                """
            )

        conn.commit()
        print("columns added")

    finally:
        conn.close()


if __name__ == "__main__":
    main()