"""Run one explicit staging ingestion with a fake email provider.

This module is staging/dev-only. It is never started by the application.
"""

import asyncio
from datetime import datetime, timezone
import os

from dotenv import load_dotenv
from sqlalchemy.engine import make_url


EXTERNAL_MESSAGE_ID = "ohmysmell-staging-test-order-v1"
SUBJECT = "STAGING TEST ORDER"
BODY = (
    "Chanel Allure Homme Sport 2 шт\n"
    "Marvis Classic Strong Mint 85 ml - 3"
)


def configure_staging_environment() -> None:
    load_dotenv(".env")
    if os.getenv("EXTERNAL_WRITES_ENABLED", "false").lower() in {
        "1", "true", "yes", "on",
    }:
        raise RuntimeError(
            "EXTERNAL_WRITES_ENABLED must be false for staging fake ingestion"
        )

    production_raw = os.getenv("DATABASE_URL")
    staging_raw = os.getenv("STAGING_DATABASE_URL")
    if not production_raw or not staging_raw:
        raise RuntimeError(
            "DATABASE_URL and STAGING_DATABASE_URL must both be configured"
        )
    production = make_url(production_raw)
    staging = make_url(staging_raw)
    if (production.host, production.database) == (
        staging.host,
        staging.database,
    ):
        raise RuntimeError("Staging database must differ from production")

    # Database modules imported after this point bind only to staging.
    os.environ["DATABASE_URL"] = staging_raw


class TelegramResultTracker:
    def __init__(self) -> None:
        self.sent_count = 0

    async def __call__(self, draft) -> None:
        from app.services.draft_telegram_service import (
            notify_managers_about_draft,
        )

        self.sent_count = await notify_managers_about_draft(draft)


async def run_once() -> None:
    from app.integrations.email.provider import EmailMessage, FakeEmailProvider
    from app.repositories.draft_order_repository import DraftOrderRepository
    from app.services.draft_order_service import DraftOrderService
    from app.workers.email_ingestion import EmailIngestionWorker

    message = EmailMessage(
        external_message_id=EXTERNAL_MESSAGE_ID,
        sender_email="staging-test@ohmysmell.local",
        sender_name="OhMySmell Staging Test",
        subject=SUBJECT,
        body_text=BODY,
        received_at=datetime.now(timezone.utc),
    )
    provider = FakeEmailProvider([message])
    tracker = TelegramResultTracker()
    pipeline = DraftOrderService(notifier=tracker)
    worker = EmailIngestionWorker(
        provider=provider,
        pipeline=pipeline,
        provider_name="staging_fake",
    )

    stats = await worker.run_once()
    draft = await DraftOrderRepository().get_by_external_message_id(
        EXTERNAL_MESSAGE_ID
    )
    if draft is None:
        raise RuntimeError("Staging ingestion did not create or find a draft")

    print(f"received={stats['received']} processed={stats['processed']} failed={stats['failed']}")
    print(f"draft_id={draft.id} status={draft.status}")
    for item in draft.items:
        print(
            "item="
            f"{item.raw_product_text!r} qty={item.qty} "
            f"match_status={item.match_status} product_id={item.product_id or '-'}"
        )
    print(f"telegram_messages_sent={tracker.sent_count}")
    print("next=Use Telegram manager callbacks to review and finalize the draft")


def main() -> None:
    configure_staging_environment()
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
