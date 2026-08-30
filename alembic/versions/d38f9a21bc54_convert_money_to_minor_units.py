"""convert money columns to integer minor units

Revision ID: d38f9a21bc54
Revises: c27e8f10ab43
Create Date: 2026-08-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d38f9a21bc54"
down_revision: Union[str, Sequence[str], None] = "c27e8f10ab43"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


MONEY_COLUMNS = (
    ("orders", "total", False),
    ("order_items", "price", False),
    ("order_items", "item_total", False),
    ("draft_orders", "total", True),
    ("draft_order_items", "price", True),
    ("draft_order_items", "item_total", True),
)


def upgrade() -> None:
    for table, column, nullable in MONEY_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=nullable,
            postgresql_using=f"{column}::bigint",
        )
    for table, column, _nullable in MONEY_COLUMNS:
        op.execute(
            sa.text(
                f'UPDATE "{table}" SET "{column}" = "{column}" * 100 '
                f'WHERE "{column}" IS NOT NULL'
            )
        )


def downgrade() -> None:
    for table, column, _nullable in MONEY_COLUMNS:
        op.execute(
            sa.text(
                f'UPDATE "{table}" SET "{column}" = "{column}" / 100 '
                f'WHERE "{column}" IS NOT NULL'
            )
        )
    for table, column, nullable in MONEY_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=nullable,
            postgresql_using=f"{column}::integer",
        )
