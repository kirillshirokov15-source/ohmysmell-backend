from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class MessageProcessingStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class InboundMessage(Base):
    __tablename__ = "inbound_messages"
    __table_args__ = (
        CheckConstraint("source IN ('email')", name="ck_inbound_messages_source"),
        CheckConstraint(
            "processing_status IN ('received', 'processing', 'processed', 'failed')",
            name="ck_inbound_messages_processing_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    external_message_id: Mapped[str] = mapped_column(
        String(512), nullable=False, unique=True, index=True
    )
    sender: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    processing_status: Mapped[MessageProcessingStatus] = mapped_column(
        String(20),
        nullable=False,
        default=MessageProcessingStatus.RECEIVED,
        server_default=MessageProcessingStatus.RECEIVED.value,
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
