from pydantic import BaseModel, Field


class OrderItem(BaseModel):
    id: str
    qty: int = Field(gt=0)


class OrderCreate(BaseModel):
    customer_name: str
    phone: str
    telegram: str | None = None
    comment: str | None = None
    items: list[OrderItem]