"""add email provider cursor

Revision ID: e49a0b32cd65
Revises: d38f9a21bc54
Create Date: 2026-08-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e49a0b32cd65"
down_revision: Union[str, Sequence[str], None] = "d38f9a21bc54"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_provider_cursors",
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("cursor_value", sa.String(255), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("provider"),
    )


def downgrade() -> None:
    op.drop_table("email_provider_cursors")
