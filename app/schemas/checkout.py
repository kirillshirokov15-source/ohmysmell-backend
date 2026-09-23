from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class CheckoutItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product_id: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")
    qty: int = Field(strict=True, gt=0, le=10000)


class CheckoutCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=320)
    phone: str = Field(min_length=7, max_length=32, pattern=r"^\+?[0-9 ()-]+$")
    comment: str | None = Field(default=None, max_length=2000)
    items: list[CheckoutItem] = Field(min_length=1, max_length=100)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        import re
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Некорректный email")
        return value.casefold()


class CheckoutResponse(BaseModel):
    request_id: str
    status: Literal["needs_review"] = "needs_review"
    currency: Literal["RUB"] = "RUB"
    total_minor: int | None
    message: str


class PublicProduct(BaseModel):
    id: str
    name: str
    article: str | None = None
    description: str | None = None
    price_minor: int | None
    currency: Literal["RUB"] = "RUB"
    available: bool
    requires_review: bool
    availability_state: Literal["in_stock", "on_request", "confirmed", "unavailable"] = "in_stock"


class CatalogResponse(BaseModel):
    products: list[PublicProduct]
    offset: int
    limit: int
    total: int


class ErrorField(BaseModel):
    loc: list[str | int]
    type: str


class ErrorDetail(BaseModel):
    code: str
    message: str | None = None
    fields: list[ErrorField] | None = None


class ApiError(BaseModel):
    detail: ErrorDetail
