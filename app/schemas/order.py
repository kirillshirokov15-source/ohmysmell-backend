from pydantic import BaseModel
from typing import Optional


class OrderItem(BaseModel):
    id: int
    brand: str
    name: str
    price: int
    qty: int


class OrderCreate(BaseModel):
    customer_name: str
    phone: str
    telegram: Optional[str] = None
    comment: Optional[str] = None

    items: list[OrderItem]