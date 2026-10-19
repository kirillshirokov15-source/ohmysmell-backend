"""Separate owned physical X inventory from historical partner settlements."""
from alembic import op
import sqlalchemy as sa
revision = "t93bf087ad10"
down_revision = "s82ae976fc09"
branch_labels = depends_on = None


def upgrade():
    op.add_column("warehouse_allocations", sa.Column("own_cost_minor", sa.BigInteger()))
    op.drop_constraint("ck_allocation_source", "warehouse_allocations", type_="check")
    op.create_check_constraint("ck_allocation_source", "warehouse_allocations",
        "source_type IN ('own','own_x','partner_x','external')")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM warehouse_allocations WHERE source_type='own_x'")):
        raise RuntimeError("Owned X audit must be retained")
    op.drop_constraint("ck_allocation_source", "warehouse_allocations", type_="check")
    op.create_check_constraint("ck_allocation_source", "warehouse_allocations", "source_type IN ('own','partner_x','external')")
    op.drop_column("warehouse_allocations", "own_cost_minor")
