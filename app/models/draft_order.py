from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.models.sales import CustomerType, OrderSource
from app.services.order_lifecycle import OrderStatus


class ProductMatchStatus(StrEnum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class DraftOrder(Base):
    __tablename__ = "draft_orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'needs_review', 'ready', 'new', 'rejected')",
            name="ck_draft_orders_status",
        ),
        CheckConstraint(
            "customer_type IN ('wholesale', 'retail', 'unknown')",
            name="ck_draft_orders_customer_type",
        ),
        CheckConstraint("source IN ('email', 'website', 'instagram', 'manual', 'telegram')", name="ck_draft_orders_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    contact_details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    inbound_message_id: Mapped[int] = mapped_column(
        ForeignKey("inbound_messages.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    customer_type: Mapped[CustomerType] = mapped_column(String(20), nullable=False)
    source: Mapped[OrderSource] = mapped_column(
        String(20), nullable=False, server_default=OrderSource.EMAIL.value
    )
    status: Mapped[OrderStatus] = mapped_column(
        String(30), nullable=False, server_default=OrderStatus.DRAFT.value
    )
    sender_email: Mapped[str] = mapped_column(String(320), nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    counterparty_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    counterparty_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    counterparty_candidates: Mapped[list[dict]] = mapped_column(
        JSON, nullable=False, default=list
    )
    total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    finalized_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    items: Mapped[list["DraftOrderItem"]] = relationship(
        back_populates="draft_order", cascade="all, delete-orphan"
    )


class DraftOrderItem(Base):
    __tablename__ = "draft_order_items"
    __table_args__ = (
        CheckConstraint(
            "match_status IN ('matched', 'ambiguous', 'not_found')",
            name="ck_draft_order_items_match_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_order_id: Mapped[int] = mapped_column(
        ForeignKey("draft_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    raw_product_text: Mapped[str] = mapped_column(Text, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_confidence: Mapped[str] = mapped_column(String(20), nullable=False, default="confirmed", server_default="confirmed")
    quantity_evidence: Mapped[str | None] = mapped_column(String(500))
    match_status: Mapped[ProductMatchStatus] = mapped_column(String(20), nullable=False)
    product_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_name: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    article: Mapped[str | None] = mapped_column(String(255), nullable=True)
    price: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    item_total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    candidates: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    draft_order: Mapped[DraftOrder] = relationship(back_populates="items")


class DraftQuantityEvent(Base):
    __tablename__ = "draft_quantity_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("draft_orders.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("draft_order_items.id"))
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger)
    old_quantity: Mapped[int] = mapped_column(Integer)
    new_quantity: Mapped[int] = mapped_column(Integer)
    old_confidence: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
