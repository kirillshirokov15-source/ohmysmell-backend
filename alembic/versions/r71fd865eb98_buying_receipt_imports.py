"""Durable email imports, supplier confirmation and shared purchase allocations."""
from alembic import op
import sqlalchemy as sa

revision = 'r71fd865eb98'
down_revision = 'q60ec754da87'
branch_labels = depends_on = None


def upgrade():
    op.add_column('buying_imports', sa.Column('source_key', sa.String(64)))
    op.create_unique_constraint('uq_buying_imports_source_key', 'buying_imports', ['source_key'])
    op.drop_constraint('ck_buying_purchase_status', 'buying_purchases', type_='check')
    op.create_check_constraint('ck_buying_purchase_status', 'buying_purchases', "status IN ('draft','sent','confirmed','received','cancelled','error')")
    op.drop_constraint('uq_allocation_buying_purchase', 'warehouse_allocations', type_='unique')
    op.create_index('ix_warehouse_allocations_buying_purchase_id', 'warehouse_allocations', ['buying_purchase_id'])


def downgrade():
    c=op.get_bind()
    if c.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM buying_purchases WHERE status='confirmed')")):
        raise RuntimeError('Confirmed purchases require forward reconciliation')
    if c.scalar(sa.text('SELECT EXISTS(SELECT 1 FROM warehouse_allocations WHERE buying_purchase_id IS NOT NULL GROUP BY buying_purchase_id HAVING count(*)>1)')):
        raise RuntimeError('Shared purchase allocations prevent downgrade')
    if c.scalar(sa.text('SELECT EXISTS(SELECT 1 FROM buying_imports WHERE source_key IS NOT NULL)')):
        raise RuntimeError('Email idempotency history must not be discarded')
    op.drop_index('ix_warehouse_allocations_buying_purchase_id', table_name='warehouse_allocations')
    op.create_unique_constraint('uq_allocation_buying_purchase', 'warehouse_allocations', ['buying_purchase_id'])
    op.drop_constraint('ck_buying_purchase_status', 'buying_purchases', type_='check')
    op.create_check_constraint('ck_buying_purchase_status', 'buying_purchases', "status IN ('draft','sent','received','cancelled','error')")
    op.drop_column('buying_imports','source_key')
