from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.models.sales import CustomerType, OrderSource
from app.services.order_lifecycle import OrderStatus


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "customer_type IN ('wholesale', 'retail', 'unknown')",
            name="ck_orders_customer_type",
        ),
        CheckConstraint(
            "source IN ('email', 'instagram', 'website', 'manual')",
            name="ck_orders_source",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
    )

    customer_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

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
