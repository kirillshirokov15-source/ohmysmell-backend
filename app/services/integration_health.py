"""A worker heartbeat stores only a fixed status code, never provider errors."""
from app.database.session import async_session
from app.models.buying import IntegrationHealth
from app.services.buying import now


async def record_email(status, delay):
    await record_mailbox('customer', status, delay)


async def record_mailbox(role, status, delay):
    if role not in ('customer', 'supplier'):
        raise ValueError('Unknown mailbox role')
    async with async_session() as session, session.begin():
        row = await session.get(IntegrationHealth, role + '_gmail')
        if row is None:
            row = IntegrationHealth(name=role + '_gmail')
            session.add(row)
        row.status, row.checked_at, row.retry_after_seconds = status, now(), delay
