"""add sales core fields to orders

Revision ID: a84f1b92c301
Revises: d678cecfa969
Create Date: 2026-08-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a84f1b92c301"
down_revision: Union[str, Sequence[str], None] = "d678cecfa969"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "customer_type",
            sa.String(length=20),
            server_default="retail",
            nullable=False,
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "source",
            sa.String(length=20),
            server_default="website",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_orders_customer_type",
        "orders",
        "customer_type IN ('wholesale', 'retail', 'unknown')",
    )
    op.create_check_constraint(
        "ck_orders_source",
        "orders",
        "source IN ('email', 'instagram', 'website', 'manual')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_orders_source", "orders", type_="check")
    op.drop_constraint("ck_orders_customer_type", "orders", type_="check")
    op.drop_column("orders", "source")
    op.drop_column("orders", "customer_type")
