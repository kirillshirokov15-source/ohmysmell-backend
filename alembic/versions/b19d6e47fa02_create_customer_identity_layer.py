"""create customer identity layer

Revision ID: b19d6e47fa02
Revises: a84f1b92c301
Create Date: 2026-08-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b19d6e47fa02"
down_revision: Union[str, Sequence[str], None] = "a84f1b92c301"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "orders",
        "customer_type",
        existing_type=sa.String(length=20),
        server_default="retail",
        existing_nullable=False,
    )
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "customer_type",
            sa.String(length=20),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("moysklad_counterparty_id", sa.String(255), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=True),
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
            "customer_type IN ('wholesale', 'retail', 'unknown')",
            name="ck_customers_customer_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("moysklad_counterparty_id"),
    )
    op.create_table(
        "customer_identities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("identity_type", sa.String(20), nullable=False),
        sa.Column("normalized_value", sa.String(320), nullable=False),
        sa.Column("original_value", sa.String(320), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "identity_type IN ('email', 'phone', 'instagram', 'telegram')",
            name="ck_customer_identities_type",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "identity_type",
            "normalized_value",
            name="uq_customer_identities_type_normalized",
        ),
    )
    op.create_index(
        "ix_customer_identities_customer_id",
        "customer_identities",
        ["customer_id"],
    )
    op.add_column("orders", sa.Column("customer_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_orders_customer_id_customers",
        "orders",
        "customers",
        ["customer_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])


def downgrade() -> None:
    op.drop_index("ix_orders_customer_id", table_name="orders")
    op.drop_constraint(
        "fk_orders_customer_id_customers", "orders", type_="foreignkey"
    )
    op.drop_column("orders", "customer_id")
    op.drop_index(
        "ix_customer_identities_customer_id",
        table_name="customer_identities",
    )
    op.drop_table("customer_identities")
    op.drop_table("customers")
    op.alter_column(
        "orders",
        "customer_type",
        existing_type=sa.String(length=20),
        server_default="wholesale",
        existing_nullable=False,
    )
