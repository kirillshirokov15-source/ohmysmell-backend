from sqlalchemy import select

from app.database.session import async_session
from app.models.manager import Manager


class ManagerRepository:
    async def is_active_by_telegram_id(self, telegram_id: int) -> bool:
        async with async_session() as session:
            result = await session.execute(
                select(Manager.id)
                .where(
                    Manager.telegram_id == telegram_id,
                    Manager.is_active.is_(True),
                )
                .limit(1)
            )
            return result.scalar_one_or_none() is not None
