from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.models.sales import CustomerType


class InboundEmailCreate(BaseModel):
    external_message_id: str = Field(min_length=1, max_length=512)
    sender_email: str = Field(min_length=3, max_length=320)
    sender_name: str | None = Field(default=None, max_length=255)
    subject: str | None = Field(default=None, max_length=1000)
    body_text: str = Field(max_length=200000)
    received_at: datetime | None = None

    def received_timestamp(self) -> datetime:
        return self.received_at or datetime.now(timezone.utc)


class SetCustomerTypeRequest(BaseModel):
    customer_type: CustomerType


class ResolveProductRequest(BaseModel):
    product_id: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")


class LinkCounterpartyRequest(BaseModel):
    counterparty_id: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")
    counterparty_name: str = Field(min_length=1, max_length=255)
