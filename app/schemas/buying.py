"""Explicit SPA response allowlists; procurement data only."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel


class BuyingUserRead(BaseModel):
    id: int
    username: str
    role: Literal['manager', 'picker']


class BuyingLoginRead(BaseModel):
    access_token: str
    token_type: Literal['bearer']
    expires_in: int


class OfferRead(BaseModel):
    id: int
    product_id: str
    supplier_id: int
    supplier_name: str
    name: str
    supplier_sku: str | None
    purchase_price_minor: int
    currency_code: Literal['RUB','USD']
    approximate_rub_minor: int | None
    approximate: bool
    fx_source: str | None
    fx_rate_date: str | None
    fx_rate_to_rub: str | None
    price_list_updated_at: str | None
    availability: Literal['on_request','confirmed','unavailable']
    active: bool
    moysklad_match_state: Literal['local','mapped']


class ProductRead(BaseModel):
    id: str
    name: str
    offers: list[OfferRead]


class CatalogRead(BaseModel):
    products: list[ProductRead]
    total: int
    offset: int
    limit: int


class OffersRead(BaseModel):
    offers: list[OfferRead]


class CartItemRead(BaseModel):
    offer: OfferRead
    quantity: int
    price_changed_since_added: bool
    created_at: str
    updated_at: str


class CartRead(BaseModel):
    items: list[CartItemRead]


class PurchaseItemRead(BaseModel):
    offer_id: int
    product_id: str
    name: str
    quantity: int
    unit_price_minor: int
    approximate_rub_minor: int | None
    fx_rate_to_rub: str | None
    fx_source: str | None
    fx_rate_date: str | None


class SupplierPreviewRead(BaseModel):
    supplier_id: int
    supplier_name: str
    recipient: str
    items: list[PurchaseItemRead]
    currency: Literal['RUB','USD']
    total_minor: int
    approximate_rub_minor: int | None
    approximate: bool
    email_body: str


class CheckoutPreviewRead(BaseModel):
    fingerprint: str
    suppliers: list[SupplierPreviewRead]


class PurchaseRead(SupplierPreviewRead):
    received_by_user_id: int | None
    received_by_role: str | None
    received_by_username: str | None
    id: int
    item_count: int
    number: str
    status: Literal['draft','sent','received','cancelled','error']
    send_state: str
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None
    received_at: datetime | None
    message_id: str | None
    thread_id: str | None
    external_ids: dict[str,str]


class ReplyRead(BaseModel):
    message_id: str
    thread_id: str
    received_at: datetime
    subject: str
    body: str
    attachments: list[dict]


class PurchaseDetailRead(PurchaseRead):
    replies: list[ReplyRead]


class PurchasesRead(BaseModel):
    purchases: list[PurchaseRead]
    total: int
    offset: int
    limit: int


class PickupItemRead(BaseModel):
    name: str
    quantity: int


class PickupRead(BaseModel):
    id: int
    number: str
    supplier_name: str
    pickup_address: str | None
    phone: str | None
    pickup_notes: str | None
    items: list[PickupItemRead]
    sent_at: datetime | None
    status: Literal['sent', 'received']
    received_at: datetime | None
    received_by_user_id: int | None
    received_by_role: str | None
    received_by_username: str | None


class PickupsRead(BaseModel):
    pickups: list[PickupRead]
    total: int
    offset: int
    limit: int
