"""Future Instagram/client Telegram adapters submit this trusted server envelope.

Telegram client identity uses the existing manual source until a dedicated source
is approved; it remains independent of customer_type. Adapters authenticate and
verify sender identities before entering the sales core.
"""
from dataclasses import dataclass
from typing import Protocol
from app.models.sales import OrderSource


@dataclass(frozen=True)
class ChannelMessage:
    source: OrderSource
    external_id: str
    identity_type: str
    identity_value: str
    text: str
    display_name: str | None = None


class ChannelAdapter(Protocol):
    async def receive(self) -> list[ChannelMessage]: ...


class ChannelIngestion(Protocol):
    async def ingest(self, message: ChannelMessage): ...
