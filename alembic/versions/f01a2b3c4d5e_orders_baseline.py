"""reconcile historical orders tables

Revision ID: f01a2b3c4d5e
Revises: None
Create Date: 2026-08-30

This additive baseline supports both fresh databases and legacy databases where
orders/order_items were created by scripts. It intentionally does not drop or
rewrite existing objects.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f01a2b3c4d5e"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "orders" not in tables:
        op.create_table(
            "orders",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("customer_name", sa.String(255), nullable=False),
            sa.Column("phone", sa.String(100), nullable=False),
            sa.Column("telegram", sa.String(255), nullable=True),
            sa.Column("counterparty_id", sa.String(255), nullable=True),
            sa.Column("counterparty_name", sa.String(255), nullable=True),
            sa.Column("comment", sa.Text(), nullable=True),
            sa.Column("status", sa.String(50), nullable=False),
            sa.Column("total", sa.Integer(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("moysklad_order_id", sa.String(255), nullable=True),
            sa.Column("moysklad_order_name", sa.String(255), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    else:
        existing = _column_names(inspector, "orders")
        optional_columns = {
            "counterparty_id": sa.Column("counterparty_id", sa.String(255)),
            "counterparty_name": sa.Column("counterparty_name", sa.String(255)),
            "moysklad_order_id": sa.Column("moysklad_order_id", sa.String(255)),
            "moysklad_order_name": sa.Column(
                "moysklad_order_name", sa.String(255)
            ),
        }
        for name, column in optional_columns.items():
            if name not in existing:
                op.add_column("orders", column)

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "order_items" not in tables:
        op.create_table(
            "order_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("order_id", sa.Integer(), nullable=False),
            sa.Column("product_id", sa.String(255), nullable=False),
            sa.Column("name", sa.String(1000), nullable=False),
            sa.Column("article", sa.String(255), nullable=True),
            sa.Column("price", sa.Integer(), nullable=False),
            sa.Column("qty", sa.Integer(), nullable=False),
            sa.Column("item_total", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["order_id"], ["orders.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_order_items_order_id", "order_items", ["order_id"])


def downgrade() -> None:
    # Reconciliation objects may predate Alembic, so downgrade is intentionally
    # non-destructive. Removing them requires an explicit manual operation.
    pass
