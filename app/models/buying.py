"""Shared procurement workspace. No customer/order references."""
from datetime import datetime
from sqlalchemy import String, Integer, BigInteger, Boolean, DateTime, ForeignKey, JSON, CheckConstraint, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.database.base import Base
from app.models.supply import Timestamps


class BuyingUser(Timestamps, Base):
    __tablename__ = "buying_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("role IN ('manager','picker')", name="ck_buying_user_role"),)


class IntegrationHealth(Base):
    __tablename__ = 'integration_health'
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    status: Mapped[str] = mapped_column(String(40))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retry_after_seconds: Mapped[int] = mapped_column(Integer, default=0)


class BuyingSession(Base):
    __tablename__ = "buying_sessions"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("buying_users.id"), index=True)


class BuyingSupplier(Base):
    __tablename__ = "buying_suppliers"
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3))
    parser: Mapped[dict] = mapped_column(JSON)
    pickup_address: Mapped[str | None] = mapped_column(String(1000))
    phone: Mapped[str | None] = mapped_column(String(80))
    pickup_notes: Mapped[str | None] = mapped_column(String(2000))
    __table_args__ = (CheckConstraint("currency IN ('RUB','USD')", name="ck_buying_currency"),)


class BuyingProduct(Base):
    __tablename__ = "buying_products"
    product_id: Mapped[str] = mapped_column(ForeignKey("product_supply.product_id"), primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    normalized_name: Mapped[str] = mapped_column(String(500), index=True)


class BuyingOffer(Base):
    __tablename__ = "buying_offers"
    offer_id: Mapped[int] = mapped_column(ForeignKey("supplier_offers.id"), primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    mapping_key: Mapped[str] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(500))
    __table_args__ = (UniqueConstraint("supplier_id", "mapping_key", name="uq_buying_mapping"),)


class BuyingImport(Base):
    __tablename__ = "buying_imports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(3))
    parser_version: Mapped[str] = mapped_column(String(40))
    rows: Mapped[list] = mapped_column(JSON)
    counts: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BuyingPriceHistory(Base):
    __tablename__ = "buying_price_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("supplier_offers.id"), index=True)
    old_price_minor: Mapped[int] = mapped_column(BigInteger)
    new_price_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    import_id: Mapped[str | None] = mapped_column(ForeignKey("buying_imports.id"))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BuyingCart(Timestamps, Base):
    __tablename__ = "buying_cart"
    offer_id: Mapped[int] = mapped_column(ForeignKey("supplier_offers.id"), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)
    added_price_minor: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (CheckConstraint("quantity > 0 AND quantity <= 100000", name="ck_buying_cart_qty"),)


class BuyingCheckout(Base):
    __tablename__ = "buying_checkouts"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    purchase_ids: Mapped[list] = mapped_column(JSON)


class BuyingPurchase(Timestamps, Base):
    __tablename__ = "buying_purchases"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20))
    send_state: Mapped[str] = mapped_column(String(20))
    message_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    supplier_mailbox_account: Mapped[str | None] = mapped_column(String(320))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("buying_users.id"))
    received_by_role: Mapped[str | None] = mapped_column(String(20))
    received_by_username: Mapped[str | None] = mapped_column(String(80))
    external_ids: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (CheckConstraint("status IN ('draft','sent','received','cancelled','error')", name="ck_buying_purchase_status"),)


class BuyingReply(Base):
    __tablename__ = "buying_replies"
    message_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(255))
    purchase_id: Mapped[int] = mapped_column(ForeignKey("buying_purchases.id"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(String(20000))
    attachments: Mapped[list] = mapped_column(JSON)


class BuyingEvent(Base):
    __tablename__ = "buying_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("buying_purchases.id"), index=True)
    action: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(100))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
