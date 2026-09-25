import asyncio
import logging
import hashlib

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
        from app.integrations.email.health import EmailDatabaseUnavailable
        try:
            cursor = await self.cursor_repository.get(self.provider_name)
        except Exception as error:
            raise EmailDatabaseUnavailable('Email cursor database unavailable') from error
        batch = await self.provider.fetch_unprocessed(cursor)
        stats = {"received": len(batch.messages), "processed": 0, "failed": 0}
        for message in batch.messages:
            message_ref = hashlib.sha256(message.external_message_id.encode()).hexdigest()[:24]
            log_event(logger, "email_received", message_ref=message_ref)
            try:
                draft = await self.pipeline.ingest_email(message)
                acknowledge = getattr(self.provider, "acknowledge", None)
                if acknowledge is not None:
                    acknowledge(message.external_message_id)
                stats["processed"] += 1
                log_event(
                    logger,
                    "email_ingested",
                    message_ref=message_ref,
                    draft_id=draft.id,
                )
            except Exception as error:
                stats["failed"] += 1
                log_event(logger, "email_ingestion_error", level=logging.ERROR,
                          message_ref=message_ref, result=type(error).__name__)
        if batch.next_cursor and not stats["failed"]:
            try:
                await self.cursor_repository.set(self.provider_name, batch.next_cursor)
            except Exception as error:
                raise EmailDatabaseUnavailable('Email cursor database unavailable') from error
            log_event(logger, "email_cursor_saved", provider=self.provider_name)
        return stats

    async def run_forever(self, interval: int | None = None) -> None:
        poll_interval = interval or settings.email_poll_interval
        while True:
            try:
                await self.run_once()
            except Exception as error:
                logger.warning(
                    '{"event":"email_poll_error","error_type":"%s"}',
                    type(error).__name__,
                )
            await asyncio.sleep(poll_interval)


async def main() -> None:
    from app.workers.email_runtime import run
    await run()


if __name__ == "__main__":
    from app.workers.email_runtime import main as entrypoint
    entrypoint()
