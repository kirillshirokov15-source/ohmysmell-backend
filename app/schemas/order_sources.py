from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourcePart(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    item_id: int = Field(strict=True, gt=0)
    qty: int = Field(strict=True, gt=0, le=10000)
    warehouse_id: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")
    source_type: Literal["own", "partner_x", "external"]
    supplier_id: int | None = Field(default=None, strict=True, gt=0)
    base_cost_minor: int | None = Field(default=None, strict=True, ge=0, le=100_000_000_000)
    offer_id: int | None = Field(default=None, strict=True, gt=0)
    purchase_id: int | None = Field(default=None, strict=True, gt=0)

    @model_validator(mode="after")
    def source_fields(self):
        if self.purchase_id is not None and self.source_type != 'external':
            raise ValueError('purchase_id is only valid for external supply')
        if self.source_type == "partner_x":
            if self.base_cost_minor is None or self.offer_id is not None:
                raise ValueError("Для единого партнёра Х нужна закупочная цена, без offer_id")
        elif self.supplier_id is not None or self.base_cost_minor is not None:
            raise ValueError("supplier_id и base_cost_minor используются только для X")
        if (self.source_type == "external") != (self.offer_id is not None):
            raise ValueError("offer_id нужен только для внешнего поставщика")
        return self


class SourceAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["assign", "receive", "post_receipt", "cancel"]
    expected_revision: int = Field(strict=True, ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    parts: list[SourcePart] = Field(default_factory=list, max_length=100)
    allocation_id: int | None = Field(default=None, strict=True, gt=0)

    @model_validator(mode="after")
    def action_fields(self):
        if self.action == "assign":
            if not self.parts or self.allocation_id is not None:
                raise ValueError("Для назначения нужны parts без allocation_id")
        elif self.parts or self.allocation_id is None:
            raise ValueError("Для операции нужен allocation_id без parts")
        return self
