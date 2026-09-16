"""Add local operations and Telegram client intake; preserve existing orders."""
from alembic import op
import sqlalchemy as sa
revision = "j93d5087bc10"
down_revision = "i82c4f76ab09"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("request_key", sa.String(128), nullable=True))
    op.add_column("orders", sa.Column("request_hash", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_orders_request_key", "orders", ["request_key"])
    op.add_column("orders", sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
    for name, default in (("fulfillment_status", "new"), ("payment_status", "unpaid"), ("delivery_method", "unselected"), ("delivery_status", "pending")):
        op.add_column("orders", sa.Column(name, sa.String(20), nullable=False, server_default=default))
    op.add_column("orders", sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false()))
    for name in ("status_changed_at", "assembling_at", "assembled_at", "shipped_at", "paid_at"):
        op.add_column("orders", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    for name in ("status_changed_by_manager_id", "paid_by_manager_id"):
        op.add_column("orders", sa.Column(name, sa.Integer(), sa.ForeignKey("managers.id"), nullable=True))
    op.add_column("orders", sa.Column("payment_note", sa.String(1000), nullable=True))
    op.add_column("orders", sa.Column("delivery_reference", sa.String(255), nullable=True))
    for name in ("fulfillment_status", "payment_status"):
        op.create_index("ix_orders_" + name, "orders", [name])
    for name, expression in {
        "fulfillment": "fulfillment_status IN ('new', 'assembling', 'assembled', 'shipped', 'cancelled')",
        "payment": "payment_status IN ('unpaid', 'paid')",
        "delivery_method": "delivery_method IN ('unselected', 'pickup', 'manual', 'cdek', 'yandex')",
        "delivery_status": "delivery_status IN ('pending', 'ready', 'dispatched', 'delivered', 'cancelled')",
    }.items():
        op.create_check_constraint("ck_orders_" + name, "orders", expression)
    op.execute("UPDATE orders SET fulfillment_status = 'cancelled', delivery_status = 'cancelled' WHERE status = 'rejected'")
    for table in ("orders", "draft_orders", "inbound_messages"):
        op.drop_constraint("ck_" + table + "_source", table, type_="check")
        op.create_check_constraint("ck_" + table + "_source", table, "source IN ('email', 'website', 'instagram', 'manual', 'telegram')")
    op.create_table("order_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("manager_id", sa.Integer(), sa.ForeignKey("managers.id"), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("before", sa.JSON(), nullable=False), sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("order_id", "idempotency_key", name="uq_order_event_key"))
    op.create_index("ix_order_events_order_id", "order_events", ["order_id"])
    op.create_table("client_conversations",
        sa.Column("telegram_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("state", sa.String(20), nullable=False), sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("client_updates", sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("response", sa.String(4000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))


def downgrade():
    raise RuntimeError("Preserve operational audit; use a reviewed forward migration")
