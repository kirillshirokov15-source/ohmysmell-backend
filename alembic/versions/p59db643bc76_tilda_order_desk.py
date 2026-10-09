"""Add order-scoped Tilda identity, links, assignment audit and Telegram outbox."""
from alembic import op
import sqlalchemy as sa

revision = "p59db643bc76"
down_revision = "o48ca532ab65"
branch_labels = depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("manual_fulfillment", sa.Boolean(), nullable=False, server_default="false"))
    op.create_table("order_desks",
        sa.Column("draft_id", sa.Integer(), sa.ForeignKey("draft_orders.id"), primary_key=True),
        sa.Column("external_id", sa.String(255), nullable=False, unique=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), unique=True),
        sa.Column("manager_id", sa.Integer(), sa.ForeignKey("managers.id")),
        sa.Column("actor_telegram_id", sa.BigInteger()),
        sa.Column("assigned_at", sa.DateTime(timezone=True)),
        sa.Column("customer_telegram_id", sa.BigInteger()),
        sa.Column("linked_at", sa.DateTime(timezone=True)),
        sa.Column("stage", sa.String(30), nullable=False, server_default="new"),
        sa.CheckConstraint("stage IN ('new','working','awaiting_confirmation','awaiting_payment')", name="ck_desk_stage"))
    op.create_index("ix_order_desks_customer_telegram_id", "order_desks", ["customer_telegram_id"])
    op.create_table("order_links",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("draft_id", sa.Integer(), sa.ForeignKey("order_desks.draft_id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_by", sa.BigInteger()), sa.Column("consumed_at", sa.DateTime(timezone=True)))
    op.create_index("ix_order_links_draft_id", "order_links", ["draft_id"])
    op.create_table("desk_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("draft_id", sa.Integer(), sa.ForeignKey("order_desks.draft_id"), nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger()),
        sa.Column("action", sa.String(40), nullable=False), sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_desk_events_draft_id", "desk_events", ["draft_id"])
    op.create_table("desk_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("draft_id", sa.Integer(), sa.ForeignKey("order_desks.draft_id")),
        sa.Column("idempotency_key", sa.String(180), nullable=False, unique=True),
        sa.Column("sender_type", sa.String(20), nullable=False), sa.Column("sender_telegram_id", sa.BigInteger()),
        sa.Column("source_message_id", sa.BigInteger()), sa.Column("direction", sa.String(20), nullable=False),
        sa.Column("destination", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default="text"), sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("telegram_message_id", sa.BigInteger()), sa.Column("error_code", sa.String(60)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('pending','sending','sent','failed','blocked','uncertain')", name="ck_desk_message_status"),
        sa.CheckConstraint("direction IN ('to_manager','to_customer')", name="ck_desk_message_direction"))
    op.create_index("ix_desk_messages_draft_id", "desk_messages", ["draft_id"])
    op.create_index("ix_desk_messages_status", "desk_messages", ["status"])


def downgrade():
    raise RuntimeError("Durable order links and message audit must not be dropped; restore a verified backup")
