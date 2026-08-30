from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class EmailMessage:
    external_message_id: str
    sender_email: str
    sender_name: str | None
    subject: str | None
    body_text: str
    received_at: datetime


@dataclass(frozen=True)
class EmailFetchBatch:
    messages: Sequence[EmailMessage]
    next_cursor: str | None


class EmailProvider(Protocol):
    async def fetch_unprocessed(
        self, cursor: str | None = None
    ) -> EmailFetchBatch: ...


class FakeEmailProvider:
    def __init__(self, messages: Sequence[EmailMessage] = ()) -> None:
        self.messages = list(messages)

    async def fetch_unprocessed(
        self, cursor: str | None = None
    ) -> EmailFetchBatch:
        return EmailFetchBatch(list(self.messages), cursor)
