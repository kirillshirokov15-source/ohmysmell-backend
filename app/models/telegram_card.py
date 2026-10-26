"""Only bot-generated service cards are recorded here, never user messages."""
from datetime import datetime
from sqlalchemy import BigInteger, Integer, String, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from app.database.base import Base


class TelegramCard(Base):
    __tablename__ = "telegram_cards"
    entity: Mapped[str] = mapped_column(String(40), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    digest: Mapped[str | None] = mapped_column(String(64))
    attempt_id: Mapped[int | None] = mapped_column(Integer)


class TelegramCardAttempt(Base):
    __tablename__ = "telegram_card_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity: Mapped[str] = mapped_column(String(40), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20))
    digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
