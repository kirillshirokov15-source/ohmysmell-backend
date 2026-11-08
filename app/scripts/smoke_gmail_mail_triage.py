"""No-network integration smoke for customer Gmail triage and idempotent notifier routing."""
import asyncio
import ast
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.models.draft_order import ProductMatchStatus
from app.models.sales import CustomerType
from app.services.draft_order_service import DraftOrderService
from app.services.order_extraction_service import OrderExtractionService
from app.services.email_triage import (
    is_technical_notice, kind_for_draft, should_send_order_notification,
)


class Repository:
    def __init__(self):
        self.created = []
        self.notified = []

    async def get_by_external_message_id(self, _):
        return None

    async def create(self, data):
        draft = SimpleNamespace(
            id=800 + len(self.created), source=data["source"],
            status=data["status"], contact_details=data["contact_details"],
            sender_email=data["sender_email"], subject=data["subject"],
        )
        self.created.append((draft, data))
        return draft

    async def mark_notified(self, identifier):
        self.notified.append(identifier)


class CustomerService:
    async def resolve_email(self, sender, name):
        return SimpleNamespace(
            customer_id=1, customer_type=CustomerType.WHOLESALE,
            moysklad_counterparty_id="matched_counterparty",
            identities={},
        )


class MatchingService:
    async def match_async(self, name, qty):
        return SimpleNamespace(
            status=ProductMatchStatus.NOT_FOUND,
            raw_product_text=name, qty=qty, product=None, candidates=[],
        )


def email(identifier, sender, subject, body):
    return SimpleNamespace(
        external_message_id=identifier,
        sender_email=sender,
        sender_name="Клиент",
        subject=subject,
        body_text=body,
        received_at=datetime(2026, 11, 8, tzinfo=timezone.utc),
    )


async def verify():
    repo = Repository()
    alerts = []

    async def notify(draft):
        alerts.append(draft.id)

    service = DraftOrderService(
        repository=repo,
        customer_service=CustomerService(),
        extraction_service=OrderExtractionService(),
        matching_service=MatchingService(),
        # No real credentials and no external API instantiation in this smoke.
        counterparty_service=object(),
        notifier=notify,
    )
    old_notice = email(
        "dsn", "mailer-daemon@googlemail.com",
        "Delivery Status Notification (Failure)",
        "Delivery incomplete: message could not be delivered",
    )
    question = email(
        "question", "customer@example.org", "Вопрос по наличию",
        "Подскажите, есть ли товар?",
    )
    order = email(
        "order", "buyer@example.org", "Заказ",
        "RHD028\n\nRhode Highlighting Milk 01 Pearly pink\n\n1",
    )
    d1 = await service.ingest_email(old_notice)
    d2 = await service.ingest_email(question)
    d3 = await service.ingest_email(order)
    assert [kind_for_draft(x) for x in (d1, d2, d3)] == [
        "technical", "question", "order",
    ]
    assert alerts == [d3.id], f"Only real orders may alert managers: {alerts}"
    assert repo.notified == [d1.id, d2.id, d3.id]
    assert repo.created[0][1]["body_text"] == old_notice.body_text
    assert repo.created[0][1]["contact_details"]["mail_intent"] == "system"
    assert repo.created[1][1]["contact_details"]["mail_intent"] == "inquiry"
    assert len(repo.created[2][1]["items"]) == 1
    assert repo.created[2][1]["items"][0]["qty"] == 1
    assert not is_technical_notice("client@example.org",
                                   "Delivery Status Notification (Failure)")
    os.environ["OMS_EMAIL_TRIAGE_ENABLED"] = "false"
    assert should_send_order_notification(d1)
    print("GMAIL_TRIAGE_SMOKE_OK", flush=True)


def main():
    for filename in [
        "app/services/email_triage.py",
        "app/services/draft_order_service.py",
        "app/services/draft_telegram_service.py",
    ]:
        ast.parse(Path(filename).read_text(encoding="utf-8-sig"), filename=filename)
    os.environ["OMS_EMAIL_TRIAGE_ENABLED"] = "true"
    os.environ["OMS_GMAIL_MULTILINE_SKU_ENABLED"] = "true"
    asyncio.run(verify())


if __name__ == "__main__":
    main()
