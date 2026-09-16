from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Boolean,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.models.sales import CustomerType, OrderSource
from app.services.order_lifecycle import OrderStatus


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("total >= 0", name="ck_orders_total_nonnegative"),
        CheckConstraint(
            "customer_type IN ('wholesale', 'retail', 'unknown')",
            name="ck_orders_customer_type",
        ),
        CheckConstraint(
            "source IN ('email', 'instagram', 'website', 'manual', 'telegram')",
            name="ck_orders_source",
        ),
        CheckConstraint("fulfillment_status IN ('new', 'assembling', 'assembled', 'shipped', 'cancelled')", name="ck_orders_fulfillment"),
        CheckConstraint("payment_status IN ('unpaid', 'paid')", name="ck_orders_payment"),
        CheckConstraint("delivery_method IN ('unselected', 'pickup', 'manual', 'cdek', 'yandex')", name="ck_orders_delivery_method"),
        CheckConstraint("delivery_status IN ('pending', 'ready', 'dispatched', 'delivered', 'cancelled')", name="ck_orders_delivery_status"),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
    )

    customer_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    request_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    fulfillment_status: Mapped[str] = mapped_column(String(20), default="new", server_default="new", index=True)
    payment_status: Mapped[str] = mapped_column(String(20), default="unpaid", server_default="unpaid", index=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_changed_by_manager_id: Mapped[int | None] = mapped_column(ForeignKey("managers.id"))
    assembling_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assembled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_by_manager_id: Mapped[int | None] = mapped_column(ForeignKey("managers.id"))
    payment_note: Mapped[str | None] = mapped_column(String(1000))
    delivery_method: Mapped[str] = mapped_column(String(20), default="unselected", server_default="unselected")
    delivery_status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    delivery_reference: Mapped[str | None] = mapped_column(String(255))

    customer_type: Mapped[CustomerType] = mapped_column(
        String(20),
        nullable=False,
        default=CustomerType.RETAIL,
        server_default=CustomerType.RETAIL.value,
    )

    source: Mapped[OrderSource] = mapped_column(
        String(20),
        nullable=False,
        default=OrderSource.WEBSITE,
        server_default=OrderSource.WEBSITE.value,
    )

    phone: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    telegram: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    counterparty_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    counterparty_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    comment: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=OrderStatus.NEW,
        server_default=OrderStatus.NEW.value,
    )

    total: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
    )

    moysklad_order_id: Mapped[str | None] = mapped_column(
    String(255),
    nullable=True,
    )

    moysklad_order_name: Mapped[str | None] = mapped_column(
    String(255),
    nullable=True,
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("qty > 0 AND price >= 0 AND item_total = price * qty", name="ck_order_items_money_quantity"),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
    )

    order_id: Mapped[int] = mapped_column(
        ForeignKey(
            "orders.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    product_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(1000),
        nullable=False,
    )

    article: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    price: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    qty: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    item_total: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    order: Mapped["Order"] = relationship(
        back_populates="items",
    )
