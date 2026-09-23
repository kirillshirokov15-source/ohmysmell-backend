from datetime import datetime
from typing import Literal
from pydantic import BaseModel
from app.models.sales import CustomerType, OrderSource


class DraftItemRead(BaseModel):
    id: int
    raw_product_text: str
    qty: int
    quantity_confidence: Literal["confirmed", "probable", "unknown"] = "confirmed"
    match_status: Literal["matched", "ambiguous", "not_found"]
    product_id: str | None
    product_name: str | None
    article: str | None
    price: int | None
    price_minor: int | None
    price_major: str | None
    item_total: int | None
    item_total_minor: int | None
    item_total_major: str | None
    candidates: list[dict]


class DraftRead(BaseModel):
    id: int
    revision: int
    inbound_message_id: int
    customer_id: int | None
    customer_type: CustomerType
    source: OrderSource
    status: Literal["draft", "needs_review", "ready", "new", "rejected"]
    sender_email: str
    contact_details: dict
    customer_name: str | None
    subject: str | None
    counterparty_id: str | None
    counterparty_name: str | None
    counterparty_candidates: list[dict]
    total: int | None
    total_minor: int | None
    total_major: str | None
    review_notes: str | None
    finalized_order_id: int | None
    created_at: datetime
    items: list[DraftItemRead]


class DraftListRead(BaseModel):
    draft_orders: list[DraftRead]
