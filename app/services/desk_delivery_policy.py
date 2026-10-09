"""Staging delivery is opt-in by BOTH destination and audited message/draft scope."""
import os
from dataclasses import dataclass
from datetime import datetime, timezone
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
    not_before: datetime | None = None

    @classmethod
    def load(cls, role):
        if role not in {"manager", "client"}:
            raise DeskDeliveryConfigurationError("Unknown delivery role")
        if settings.environment != "staging":
            return cls(False)
        key = "ORDER_DESK_STAGING_CLIENT_RECIPIENT_IDS" if role == "client" else "ORDER_DESK_STAGING_MANAGER_CHAT_IDS"
        drafts = ids("ORDER_DESK_STAGING_DRAFT_IDS")
        boundary = None
        raw = os.getenv("ORDER_DESK_STAGING_NOT_BEFORE", "").strip()
        if raw or drafts:
            try:
                boundary = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if boundary.tzinfo is None:
                    raise ValueError()
                boundary = boundary.astimezone(timezone.utc)
            except ValueError:
                raise DeskDeliveryConfigurationError("Staging draft scope requires a timezone-aware time boundary") from None
        recipients = ids(key, positive=role == "client")
        if role == "manager" and any(value > 0 for value in recipients):
            raise DeskDeliveryConfigurationError("Staging manager recipients must be groups")
        return cls(True, recipients, ids("ORDER_DESK_STAGING_MESSAGE_IDS"), drafts, boundary)

    def sql_scope(self):
        if not self.restricted:
            return true()
        if not self.recipients or not (self.message_ids or self.draft_ids):
            return false()
        drafts = (DeskMessage.draft_id.in_(self.draft_ids) & (DeskMessage.created_at >= self.not_before)
                  if self.not_before is not None else false())
        return DeskMessage.destination.in_(self.recipients) & or_(DeskMessage.id.in_(self.message_ids), drafts)

    def permits(self, message):
        created = getattr(message, "created_at", None)
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        in_window = (self.not_before is not None and created is not None
                     and created >= self.not_before and message.draft_id in self.draft_ids)
        return not self.restricted or (message.destination in self.recipients and
            (message.id in self.message_ids or in_window))
