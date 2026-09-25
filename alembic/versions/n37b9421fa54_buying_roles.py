"""Buying accounts, pickup contacts and durable receiving actor."""
from alembic import op
import sqlalchemy as sa

revision = "n37b9421fa54"
down_revision = "m26a8310ef43"
branch_labels = depends_on = None


def upgrade():
    op.create_table('integration_health',
        sa.Column('name', sa.String(40), primary_key=True),
        sa.Column('status', sa.String(40), nullable=False),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('retry_after_seconds', sa.Integer(), nullable=False))
    op.create_table("buying_users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(80), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('manager','picker')", name="ck_buying_user_role"))
    op.add_column("buying_sessions", sa.Column("user_id", sa.Integer(), sa.ForeignKey("buying_users.id"), nullable=True))
    op.create_index("ix_buying_sessions_user_id", "buying_sessions", ["user_id"])
    # Legacy sessions have no user and are rejected by authentication.
    for name, size in (("pickup_address", 1000), ("phone", 80), ("pickup_notes", 2000)):
        op.add_column("buying_suppliers", sa.Column(name, sa.String(size), nullable=True))
    op.add_column("buying_purchases", sa.Column("received_by_user_id", sa.Integer(), sa.ForeignKey("buying_users.id"), nullable=True))
    op.add_column("buying_purchases", sa.Column("received_by_role", sa.String(20), nullable=True))
    op.add_column("buying_purchases", sa.Column("received_by_username", sa.String(80), nullable=True))


def downgrade():
    raise RuntimeError("Account and receiving audit data is durable; restore a verified backup")
