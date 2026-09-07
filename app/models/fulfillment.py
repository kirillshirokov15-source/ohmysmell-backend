"""Local fulfillment plans; none of these records reserve external inventory."""
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database.base import Base


class Shipment(Base):
    __tablename__ = "shipments"
    __table_args__ = (
        UniqueConstraint("order_id", "warehouse_id", name="uq_shipment_order_warehouse"),
        CheckConstraint("status IN ('planned', 'exporting', 'exported', 'uncertain', 'cancelled')", name="ck_shipments_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    warehouse_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="planned", server_default="planned")
    external_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    allocations: Mapped[list["WarehouseAllocation"]] = relationship(cascade="all, delete-orphan")


class WarehouseAllocation(Base):
    __tablename__ = "warehouse_allocations"
    __table_args__ = (
        UniqueConstraint("shipment_id", "order_item_id", name="uq_allocation_shipment_item"),
        CheckConstraint("qty > 0", name="ck_warehouse_allocations_qty"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id", ondelete="CASCADE"), index=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id", ondelete="CASCADE"), index=True)
    qty: Mapped[int] = mapped_column(Integer)


class CheckoutRequest(Base):
    __tablename__ = "checkout_requests"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    draft_id: Mapped[int] = mapped_column(ForeignKey("draft_orders.id", ondelete="RESTRICT"), unique=True)
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeliveryRequest(Base):
    __tablename__ = "delivery_requests"
    __table_args__ = (
        CheckConstraint("provider IN ('cdek', 'yandex', 'manual')", name="ck_delivery_provider"),
        CheckConstraint("status IN ('draft', 'submitting', 'created', 'uncertain', 'cancelled')", name="ck_delivery_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id", ondelete="RESTRICT"), unique=True)
    provider: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default="draft")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    external_id: Mapped[str | None] = mapped_column(String(255))


class ExternalOperation(Base):
    __tablename__ = "external_operations"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    payload: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
