"""Durable per-entity, per-chat Telegram card receipts."""
from alembic import op
import sqlalchemy as sa

revision = "s82ae976fc09"
down_revision = "r71fd865eb98"
branch_labels = depends_on = None


def upgrade():
    op.create_table("telegram_cards",
        sa.Column("entity", sa.String(40), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("message_id", sa.BigInteger()), sa.Column("digest", sa.String(64)),
        sa.Column("attempt_id", sa.Integer()))
    op.create_table("telegram_card_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity", sa.String(40), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_telegram_card_attempts_entity", "telegram_card_attempts", ["entity"])
    op.create_index("ix_telegram_card_attempts_chat_id", "telegram_card_attempts", ["chat_id"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM telegram_card_attempts")):
        raise RuntimeError("Card delivery audit must be retained; use a forward migration")
    op.drop_table("telegram_card_attempts")
    op.drop_table("telegram_cards")
