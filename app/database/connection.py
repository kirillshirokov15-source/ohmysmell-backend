from sqlalchemy import text
import logging
from time import perf_counter

from app.config.settings import settings
from app.logging_utils import log_event

logger = logging.getLogger(__name__)


async def check_database_connection() -> bool:
    if not settings.database_url:
        return False

    from app.database.session import engine
    started = perf_counter()
    connected = False
    error_type = None
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        connected = True
        return True
    except Exception as error:
        error_type = type(error).__name__
        return False
    finally:
        log_event(logger, "database_health_completed", connected=connected,
                  duration_ms=round((perf_counter() - started) * 1000, 2),
                  error_type=error_type)
