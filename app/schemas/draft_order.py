from datetime import datetime, timezone

from pydantic import BaseModel

from app.models.sales import CustomerType


class InboundEmailCreate(BaseModel):
    external_message_id: str
    sender_email: str
    sender_name: str | None = None
    subject: str | None = None
    body_text: str
    received_at: datetime | None = None

    def received_timestamp(self) -> datetime:
        return self.received_at or datetime.now(timezone.utc)


class SetCustomerTypeRequest(BaseModel):
    customer_type: CustomerType


class ResolveProductRequest(BaseModel):
    product_id: str


class LinkCounterpartyRequest(BaseModel):
    counterparty_id: str
    counterparty_name: str
