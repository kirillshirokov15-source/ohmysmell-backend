from pydantic import BaseModel, Field, PrivateAttr

from app.models.sales import (
    CustomerType,
    OrderSource,
    default_customer_type,
)


class OrderItem(BaseModel):
    id: str
    qty: int = Field(gt=0)


class OrderCreate(BaseModel):
    _customer_type_explicit: bool = PrivateAttr(default=False)

    customer_name: str
    phone: str
    customer_type: CustomerType | None = None
    source: OrderSource = OrderSource.WEBSITE
    email: str | None = None
    instagram_username: str | None = None
    telegram: str | None = None
    comment: str | None = None
    items: list[OrderItem] = Field(min_length=1)

    def model_post_init(self, context: object) -> None:
        self._customer_type_explicit = "customer_type" in self.model_fields_set
        if self.customer_type is None:
            object.__setattr__(
                self,
                "customer_type",
                default_customer_type(self.source),
            )

    @property
    def customer_type_explicit(self) -> bool:
        return self._customer_type_explicit
