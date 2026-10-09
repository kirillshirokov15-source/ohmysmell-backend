"""Staging delivery is opt-in by BOTH destination and audited message/draft scope."""
import os
from dataclasses import dataclass
from sqlalchemy import false, or_, true
from app.config.settings import settings
from app.models.order_desk import DeskMessage


class DeskDeliveryConfigurationError(ValueError):
    code = "invalid_staging_delivery_scope"


def ids(name, *, positive=True):
    raw = os.getenv(name, "").strip()
    if not raw:
        return frozenset()
    try:
        parts = raw.split(",")
        if len(parts) > 100:
            raise ValueError()
        values = frozenset(int(v.strip()) for v in parts)
        if any(v == 0 or abs(v) > 9223372036854775807 or (positive and v < 0) for v in values):
            raise ValueError()
        return values
    except ValueError:
        # Do not include the original value or third-party Telegram identities.
        raise DeskDeliveryConfigurationError("Invalid staging delivery scope") from None


@dataclass(frozen=True)
class DeskDeliveryPolicy:
    restricted: bool
    recipients: frozenset[int] = frozenset()
    message_ids: frozenset[int] = frozenset()
    draft_ids: frozenset[int] = frozenset()

    @classmethod
    def load(cls, role):
        if role not in {"manager", "client"}:
            raise DeskDeliveryConfigurationError("Unknown delivery role")
        if settings.environment != "staging":
            return cls(False)
        key = "ORDER_DESK_STAGING_CLIENT_RECIPIENT_IDS" if role == "client" else "ORDER_DESK_STAGING_MANAGER_CHAT_IDS"
        return cls(True, ids(key, positive=role == "client"), ids("ORDER_DESK_STAGING_MESSAGE_IDS"),
                   ids("ORDER_DESK_STAGING_DRAFT_IDS"))

    def sql_scope(self):
        if not self.restricted:
            return true()
        if not self.recipients or not (self.message_ids or self.draft_ids):
            return false()
        return DeskMessage.destination.in_(self.recipients) & or_(
            DeskMessage.id.in_(self.message_ids), DeskMessage.draft_id.in_(self.draft_ids))

    def permits(self, message):
        return not self.restricted or (message.destination in self.recipients and
            (message.id in self.message_ids or message.draft_id in self.draft_ids))
