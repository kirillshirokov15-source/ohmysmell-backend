from datetime import datetime
from pydantic import BaseModel


class OrderItemRead(BaseModel):
    id: int
    product_id: str
    name: str
    article: str | None
    price: int
    price_major: str
    item_total: int
    item_total_major: str
    price_minor: int
    qty: int
    item_total_minor: int


class OrderRead(BaseModel):
    id: int
    customer_id: int | None
    customer_name: str
    customer_type: str
    source: str
    status: str
    revision: int
    fulfillment_status: str
    payment_status: str
    needs_review: bool
    status_changed_at: datetime | None
    status_changed_by_manager_id: int | None
    assembling_at: datetime | None
    assembled_at: datetime | None
    shipped_at: datetime | None
    paid_at: datetime | None
    paid_by_manager_id: int | None
    payment_note: str | None
    delivery_method: str
    delivery_status: str
    delivery_reference: str | None
    email: str | None
    phone: str
    telegram: str | None
    counterparty_id: str | None
    counterparty_name: str | None
    total: int
    total_minor: int
    total_major: str
    created_at: datetime
    items: list[OrderItemRead]
