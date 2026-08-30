from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.models.sales import CustomerIdentityType, CustomerType


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint(
            "customer_type IN ('wholesale', 'retail', 'unknown')",
            name="ck_customers_customer_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_type: Mapped[CustomerType] = mapped_column(
        String(20),
        nullable=False,
        default=CustomerType.UNKNOWN,
        server_default=CustomerType.UNKNOWN.value,
    )
    moysklad_counterparty_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
    )
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    identities: Mapped[list["CustomerIdentity"]] = relationship(
        back_populates="customer",
        cascade="all, delete-orphan",
    )


class CustomerIdentity(Base):
    __tablename__ = "customer_identities"
    __table_args__ = (
        UniqueConstraint(
            "identity_type",
            "normalized_value",
            name="uq_customer_identities_type_normalized",
        ),
        CheckConstraint(
            "identity_type IN ('email', 'phone', 'instagram', 'telegram')",
            name="ck_customer_identities_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    identity_type: Mapped[CustomerIdentityType] = mapped_column(
        String(20),
        nullable=False,
    )
    normalized_value: Mapped[str] = mapped_column(String(320), nullable=False)
    original_value: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    customer: Mapped[Customer] = relationship(back_populates="identities")
