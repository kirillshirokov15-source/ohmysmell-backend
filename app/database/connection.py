from sqlalchemy import text

from app.config.settings import settings


async def check_database_connection() -> bool:
    if not settings.database_url:
        return False

    from app.database.session import engine
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
