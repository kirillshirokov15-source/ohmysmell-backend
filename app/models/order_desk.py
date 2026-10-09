"""Channel metadata and durable helpdesk attached to the existing draft/order."""
from datetime import datetime
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.database.base import Base


class OrderDesk(Base):
    __tablename__ = "order_desks"
    draft_id: Mapped[int] = mapped_column(ForeignKey("draft_orders.id"), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(255), unique=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), unique=True)
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("managers.id"))
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    customer_telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stage: Mapped[str] = mapped_column(String(30), default="new", server_default="new")
    __table_args__ = (CheckConstraint("stage IN ('new','working','awaiting_confirmation','awaiting_payment')", name="ck_desk_stage"),)


class OrderLink(Base):
    __tablename__ = "order_links"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("order_desks.draft_id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_by: Mapped[int | None] = mapped_column(BigInteger)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeskEvent(Base):
    __tablename__ = "desk_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("order_desks.draft_id"), index=True)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeskMessage(Base):
    """Also the outbox: one logical text/card and one destination per row."""
    __tablename__ = "desk_messages"
    __table_args__ = (
        CheckConstraint("status IN ('pending','sending','sent','failed','blocked','uncertain')", name="ck_desk_message_status"),
        CheckConstraint("direction IN ('to_manager','to_customer')", name="ck_desk_message_direction"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("order_desks.draft_id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    sender_type: Mapped[str] = mapped_column(String(20))
    sender_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    direction: Mapped[str] = mapped_column(String(20))
    destination: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(20), default="text", server_default="text")
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
