"""merge orders baseline with application migration chain

Revision ID: f59b1c43de76
Revises: e49a0b32cd65, f01a2b3c4d5e
Create Date: 2026-08-30
"""

from typing import Sequence, Union


revision: str = "f59b1c43de76"
down_revision: Union[str, Sequence[str], None] = (
    "e49a0b32cd65",
    "f01a2b3c4d5e",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
