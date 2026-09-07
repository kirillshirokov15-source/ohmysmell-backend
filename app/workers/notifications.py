"""At-least-once Telegram delivery of durable local draft notifications."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.database.session import async_session
from app.models.notification import DraftNotification
from app.repositories.draft_order_repository import DraftOrderRepository
from app.services.draft_telegram_service import notify_managers_about_draft

logger = logging.getLogger(__name__)


async def run_once(notifier=notify_managers_about_draft):
    async with async_session() as session, session.begin():
        task = (await session.execute(select(DraftNotification).where(
            DraftNotification.status != "sent",
            DraftNotification.available_at <= datetime.now(timezone.utc),
        ).order_by(DraftNotification.id).limit(1).with_for_update(skip_locked=True))).scalar_one_or_none()
        if not task:
            return False
        task.status = "processing"
        task.attempts += 1
        task.available_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        task_id, draft_id, attempts = task.id, task.draft_id, task.attempts
    try:
        draft = await DraftOrderRepository().get(draft_id)
        if draft:
            await notifier(draft)
    except Exception as error:
        logger.warning("draft_notification_failed task_id=%s error_type=%s", task_id, type(error).__name__)
        async with async_session() as session, session.begin():
            task = await session.get(DraftNotification, task_id)
            task.status = "pending"
            task.available_at = datetime.now(timezone.utc) + timedelta(seconds=min(3600, 30 * 2 ** min(attempts, 7)))
    else:
        await DraftOrderRepository().mark_notified(draft_id)
    return True


async def main():
    while True:
        try:
            processed = await run_once()
        except Exception as error:
            logger.warning("notification_poll_failed error_type=%s", type(error).__name__)
            processed = False
        await asyncio.sleep(1 if processed else 10)


if __name__ == "__main__":
    asyncio.run(main())
