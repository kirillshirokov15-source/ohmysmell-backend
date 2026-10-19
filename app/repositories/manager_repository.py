from sqlalchemy import select

from app.database.session import async_session
from app.models.manager import Manager


class ManagerRepository:
    async def is_active_by_telegram_id(self, telegram_id: int) -> bool:
        from time import perf_counter
        from app.services.telegram_metrics import observe
        started = perf_counter()
        async with async_session() as session:
            result = await session.execute(
                select(Manager.id)
                .where(
                    Manager.telegram_id == telegram_id,
                    Manager.is_active.is_(True),
                )
                .limit(1)
            )
            active = result.scalar_one_or_none() is not None
            observe("authorization", started)
            return active
