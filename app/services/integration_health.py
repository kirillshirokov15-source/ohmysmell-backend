"""A worker heartbeat stores only a fixed status code, never provider errors."""
from app.database.session import async_session
from app.models.buying import IntegrationHealth
from app.services.buying import now


async def record_email(status, delay):
    async with async_session() as session, session.begin():
        row = await session.get(IntegrationHealth, 'gmail')
        if row is None:
            row = IntegrationHealth(name='gmail')
            session.add(row)
        row.status, row.checked_at, row.retry_after_seconds = status, now(), delay
