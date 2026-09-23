"""Local supply overlay; product_id is the existing commercial catalog identity."""
from datetime import datetime, date
from decimal import Decimal
from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, JSON, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.database.base import Base


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Supplier(Timestamps, Base):
    __tablename__ = "suppliers"
    __table_args__ = (CheckConstraint("supplier_type IN ('partner_x','external_wholesaler')", name="ck_supplier_type"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_type: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(255))
    contact: Mapped[str | None] = mapped_column(String(500))
    email: Mapped[str | None] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    external_reference: Mapped[str | None] = mapped_column(String(255))


class ProductSupply(Timestamps, Base):
    __tablename__ = "product_supply"
    __table_args__ = (CheckConstraint("source_type IN ('own','partner_x','external')", name="ck_product_supply_source"),
        CheckConstraint("(source_type = 'partner_x' AND supplier_id IS NOT NULL AND base_cost_minor IS NOT NULL AND base_cost_minor >= 0) OR (source_type != 'partner_x' AND supplier_id IS NULL AND base_cost_minor IS NULL)", name="ck_product_supply_partner"))
    product_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(20))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    base_cost_minor: Mapped[int | None] = mapped_column(BigInteger)


class SupplierOffer(Timestamps, Base):
    __tablename__ = "supplier_offers"
    __table_args__ = (CheckConstraint("purchase_price_minor >= 0 AND (availability_qty IS NULL OR availability_qty >= 0)", name="ck_offer_money_qty"),
        CheckConstraint("availability IN ('on_request','confirmed','unavailable')", name="ck_offer_availability"),
        CheckConstraint("current_fx_rate_to_rub IS NULL OR current_fx_rate_to_rub > 0", name="ck_offer_fx"))
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("product_supply.product_id"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    supplier_sku: Mapped[str | None] = mapped_column(String(255))
    purchase_price_minor: Mapped[int] = mapped_column(BigInteger)
    currency_code: Mapped[str] = mapped_column(String(3))
    availability: Mapped[str] = mapped_column(String(20), default="on_request", server_default="on_request")
    availability_qty: Mapped[int | None] = mapped_column(Integer)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    notes: Mapped[str | None] = mapped_column(String(1000))
    current_fx_rate_to_rub: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    fx_source: Mapped[str | None] = mapped_column(String(20))
    fx_rate_date: Mapped[date | None] = mapped_column(Date)
    estimated_purchase_cost_rub_minor: Mapped[int | None] = mapped_column(BigInteger)


class OrderItemSupply(Timestamps, Base):
    __tablename__ = "order_item_supply"
    __table_args__ = (CheckConstraint("source_type IN ('own','partner_x','external')", name="ck_item_supply_source"),)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id", ondelete="CASCADE"), primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    source_type: Mapped[str] = mapped_column(String(20))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    supplier_name: Mapped[str | None] = mapped_column(String(255))
    # Integer amounts and decimal strings only. Set once at X capture / procurement confirmation.
    cost_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class XSettlement(Timestamps, Base):
    __tablename__ = "x_settlements"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id", ondelete="CASCADE"), unique=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    base_cost_minor: Mapped[int] = mapped_column(BigInteger)
    sale_price_minor: Mapped[int] = mapped_column(BigInteger)
    total_margin_minor: Mapped[int] = mapped_column(BigInteger)
    partner_margin_minor: Mapped[int | None] = mapped_column(BigInteger)
    our_margin_minor: Mapped[int | None] = mapped_column(BigInteger)
    partner_due_minor: Mapped[int | None] = mapped_column(BigInteger)
    calculation_version: Mapped[str] = mapped_column(String(30), default="line-50-50-v1")
    status: Mapped[str] = mapped_column(String(40))


class ProcurementRequest(Timestamps, Base):
    __tablename__ = "procurement_requests"
    __table_args__ = (CheckConstraint("status IN ('needed','requested','confirmed','received','cancelled','unavailable')", name="ck_procurement_status"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id", ondelete="CASCADE"), unique=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("supplier_offers.id"))
    status: Mapped[str] = mapped_column(String(20), default="needed", server_default="needed")
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    manual_fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    manual_fx_by_manager_id: Mapped[int | None] = mapped_column(ForeignKey("managers.id"))
    manual_fx_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SupplyEvent(Base):
    __tablename__ = "supply_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    procurement_id: Mapped[int] = mapped_column(ForeignKey("procurement_requests.id", ondelete="CASCADE"), index=True)
    manager_id: Mapped[int] = mapped_column(ForeignKey("managers.id"))
    action: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
