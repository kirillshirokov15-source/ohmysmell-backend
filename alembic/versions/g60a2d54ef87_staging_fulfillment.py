"""Add review versioning, channel support and local fulfillment plans.

Revision ID: g60a2d54ef87
Revises: f59b1c43de76
"""
from alembic import op
import sqlalchemy as sa

revision = "g60a2d54ef87"
down_revision = "f59b1c43de76"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("draft_orders", sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
    for table in ("draft_orders", "inbound_messages"):
        op.drop_constraint(f"ck_{table}_source", table, type_="check")
        op.create_check_constraint(f"ck_{table}_source", table,
                                   "source IN ('email', 'website', 'instagram', 'manual')")
    op.create_table(
        "shipments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("warehouse_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="planned"),
        sa.Column("external_id", sa.String(255), unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("order_id", "warehouse_id", name="uq_shipment_order_warehouse"),
        sa.CheckConstraint("status IN ('planned', 'exporting', 'exported', 'uncertain', 'cancelled')", name="ck_shipments_status"),
    )
    op.create_index("ix_shipments_order_id", "shipments", ["order_id"])
    op.create_table(
        "warehouse_allocations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("shipment_id", sa.Integer(), sa.ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("order_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.UniqueConstraint("shipment_id", "order_item_id", name="uq_allocation_shipment_item"),
        sa.CheckConstraint("qty > 0", name="ck_warehouse_allocations_qty"),
    )
    for column in ("shipment_id", "order_item_id"):
        op.create_index(f"ix_warehouse_allocations_{column}", "warehouse_allocations", [column])
    op.create_table(
        "checkout_requests",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("draft_id", sa.Integer(), sa.ForeignKey("draft_orders.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "delivery_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("shipment_id", sa.Integer(), sa.ForeignKey("shipments.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("external_id", sa.String(255)),
        sa.CheckConstraint("provider IN ('cdek', 'yandex', 'manual')", name="ck_delivery_provider"),
        sa.CheckConstraint("status IN ('draft', 'submitting', 'created', 'uncertain', 'cancelled')", name="ck_delivery_status"),
    )
    op.create_table(
        "external_operations",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    # Retain data by default. Use backup restore/reviewed forward fixes.
    raise RuntimeError("This additive migration requires a reviewed rollback; automatic data deletion is disabled")
