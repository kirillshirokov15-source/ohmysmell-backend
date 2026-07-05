import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL не найден в .env")


telegram_id = 898019732
name = "Kirill"

with psycopg2.connect(DATABASE_URL) as conn:
    with conn.cursor() as cur:
        cur.execute(
    """
    INSERT INTO managers (telegram_id, name, is_active, created_at)
    VALUES (%s, %s, true, NOW())
    ON CONFLICT (telegram_id)
    DO UPDATE SET
        name = EXCLUDED.name,
        is_active = true;
    """,
    (telegram_id, name),
)

print("Manager added successfully")