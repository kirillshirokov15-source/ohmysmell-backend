"""Durable draft notifications and reconcile historical order status default."""
from alembic import op
import sqlalchemy as sa

revision = "h71b3e65fa98"
down_revision = "g60a2d54ef87"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("orders", "status", existing_type=sa.String(50), server_default="new")
    op.create_table(
        "draft_notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("draft_id", sa.Integer(), sa.ForeignKey("draft_orders.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    raise RuntimeError("Notification data must be preserved; use reviewed forward fix or backup restore")
