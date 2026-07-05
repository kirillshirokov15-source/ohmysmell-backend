import asyncpg

from app.config.settings import settings


async def check_database_connection() -> bool:
    if not settings.database_url:
        return False

    conn = await asyncpg.connect(settings.database_url)
    await conn.execute("SELECT 1")
    await conn.close()

    return True