"""add email draft pipeline

Revision ID: c27e8f10ab43
Revises: b19d6e47fa02
Create Date: 2026-08-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c27e8f10ab43"
down_revision: Union[str, Sequence[str], None] = "b19d6e47fa02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inbound_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("external_message_id", sa.String(512), nullable=False),
        sa.Column("sender", sa.String(320), nullable=False),
        sa.Column("subject", sa.String(1000), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "processing_status",
            sa.String(20),
            server_default="received",
            nullable=False,
        ),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("source IN ('email')", name="ck_inbound_messages_source"),
        sa.CheckConstraint(
            "processing_status IN ('received', 'processing', 'processed', 'failed')",
            name="ck_inbound_messages_processing_status",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_inbound_messages_external_message_id",
        "inbound_messages",
        ["external_message_id"],
        unique=True,
    )
    op.create_index(
        "ix_inbound_messages_customer_id",
        "inbound_messages",
        ["customer_id"],
    )

    op.create_table(
        "draft_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inbound_message_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("customer_type", sa.String(20), nullable=False),
        sa.Column("source", sa.String(20), server_default="email", nullable=False),
        sa.Column("status", sa.String(30), server_default="draft", nullable=False),
        sa.Column("sender_email", sa.String(320), nullable=False),
        sa.Column("customer_name", sa.String(255), nullable=True),
        sa.Column("subject", sa.String(1000), nullable=True),
        sa.Column("counterparty_id", sa.String(255), nullable=True),
        sa.Column("counterparty_name", sa.String(255), nullable=True),
        sa.Column("counterparty_candidates", sa.JSON(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("finalized_order_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'needs_review', 'ready', 'new', 'rejected')",
            name="ck_draft_orders_status",
        ),
        sa.CheckConstraint(
            "customer_type IN ('wholesale', 'retail', 'unknown')",
            name="ck_draft_orders_customer_type",
        ),
        sa.CheckConstraint("source IN ('email')", name="ck_draft_orders_source"),
        sa.ForeignKeyConstraint(
            ["inbound_message_id"], ["inbound_messages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["finalized_order_id"], ["orders.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("inbound_message_id"),
        sa.UniqueConstraint("finalized_order_id"),
    )
    op.create_index("ix_draft_orders_customer_id", "draft_orders", ["customer_id"])

    op.create_table(
        "draft_order_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("draft_order_id", sa.Integer(), nullable=False),
        sa.Column("raw_product_text", sa.Text(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("match_status", sa.String(20), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=True),
        sa.Column("product_name", sa.String(1000), nullable=True),
        sa.Column("article", sa.String(255), nullable=True),
        sa.Column("price", sa.Integer(), nullable=True),
        sa.Column("item_total", sa.Integer(), nullable=True),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "match_status IN ('matched', 'ambiguous', 'not_found')",
            name="ck_draft_order_items_match_status",
        ),
        sa.ForeignKeyConstraint(
            ["draft_order_id"], ["draft_orders.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_draft_order_items_draft_order_id",
        "draft_order_items",
        ["draft_order_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_draft_order_items_draft_order_id", table_name="draft_order_items")
    op.drop_table("draft_order_items")
    op.drop_index("ix_draft_orders_customer_id", table_name="draft_orders")
    op.drop_table("draft_orders")
    op.drop_index("ix_inbound_messages_customer_id", table_name="inbound_messages")
    op.drop_index(
        "ix_inbound_messages_external_message_id", table_name="inbound_messages"
    )
    op.drop_table("inbound_messages")
