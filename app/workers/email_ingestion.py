import asyncio
import logging

from app.config.settings import settings
from app.integrations.email.gmail_provider import GmailEmailProvider
from app.logging_utils import log_event
from app.repositories.email_cursor_repository import EmailCursorRepository
from app.services.draft_order_service import DraftOrderService


logger = logging.getLogger(__name__)


class EmailIngestionWorker:
    def __init__(
        self,
        provider=None,
        pipeline=None,
        cursor_repository=None,
        provider_name: str = "gmail",
    ) -> None:
        self.provider = provider or GmailEmailProvider()
        self.pipeline = pipeline or DraftOrderService()
        self.cursor_repository = cursor_repository or EmailCursorRepository()
        self.provider_name = provider_name

    async def run_once(self) -> dict[str, int]:
        cursor = await self.cursor_repository.get(self.provider_name)
        batch = await self.provider.fetch_unprocessed(cursor)
        stats = {"received": len(batch.messages), "processed": 0, "failed": 0}
        for message in batch.messages:
            log_event(logger, "email_received", message_id=message.external_message_id)
            try:
                draft = await self.pipeline.ingest_email(message)
                stats["processed"] += 1
                log_event(
                    logger,
                    "email_ingested",
                    message_id=message.external_message_id,
                    draft_id=draft.id,
                )
            except Exception as error:
                stats["failed"] += 1
                logger.exception(
                    '{"event":"email_ingestion_error","message_id":"%s",'
                    '"error_type":"%s"}',
                    message.external_message_id,
                    type(error).__name__,
                )
        if batch.next_cursor:
            await self.cursor_repository.set(
                self.provider_name, batch.next_cursor
            )
            log_event(logger, "email_cursor_saved", provider=self.provider_name)
        return stats

    async def run_forever(self, interval: int | None = None) -> None:
        poll_interval = interval or settings.email_poll_interval
        while True:
            try:
                await self.run_once()
            except Exception as error:
                logger.exception(
                    '{"event":"email_poll_error","error_type":"%s"}',
                    type(error).__name__,
                )
            await asyncio.sleep(poll_interval)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await EmailIngestionWorker().run_forever()


if __name__ == "__main__":
    asyncio.run(main())
