"""Quantity confidence, correction audit and Buying group delivery marker."""
from alembic import op
import sqlalchemy as sa
revision = "m26a8310ef43"
down_revision = "l15f7209de32"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('draft_order_items', sa.Column('quantity_confidence', sa.String(20), server_default='confirmed', nullable=False))
    op.add_column('draft_order_items', sa.Column('quantity_evidence', sa.String(500), nullable=True))
    op.add_column('buying_events', sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table('draft_quantity_events',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('draft_id',sa.Integer(),sa.ForeignKey('draft_orders.id'),nullable=False),
        sa.Column('item_id',sa.Integer(),sa.ForeignKey('draft_order_items.id'),nullable=False),
        sa.Column('actor_telegram_id',sa.BigInteger(),nullable=False),
        sa.Column('old_quantity',sa.Integer(),nullable=False),
        sa.Column('new_quantity',sa.Integer(),nullable=False),
        sa.Column('old_confidence',sa.String(20),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False))
    op.create_index('ix_draft_quantity_events_draft_id','draft_quantity_events',['draft_id'])


def downgrade():
    raise RuntimeError('Quantity audit is durable: restore a verified backup')
