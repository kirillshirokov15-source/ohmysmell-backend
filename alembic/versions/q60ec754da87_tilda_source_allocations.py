"""Extend existing warehouse allocations with split sources and Buying receipt links."""
from alembic import op
import sqlalchemy as sa

revision = "q60ec754da87"
down_revision = "p59db643bc76"
branch_labels = depends_on = None


def upgrade():
    op.drop_constraint("uq_allocation_shipment_item", "warehouse_allocations", type_="unique")
    for column in (
        sa.Column("managed_source", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("source_type", sa.String(20), nullable=False, server_default="own"),
        sa.Column("supply_status", sa.String(20), nullable=False, server_default="confirmed"),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id")),
        sa.Column("buying_purchase_id", sa.Integer(), sa.ForeignKey("buying_purchases.id")),
        sa.Column("receipt_simulated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("receipt_reference", sa.String(255)),
    ):
        op.add_column("warehouse_allocations", column)
    op.create_unique_constraint("uq_allocation_buying_purchase", "warehouse_allocations", ["buying_purchase_id"])
    op.create_index("uq_allocation_legacy_item", "warehouse_allocations", ["shipment_id", "order_item_id"],
                    unique=True, postgresql_where=sa.text("NOT managed_source"))
    op.create_check_constraint("ck_allocation_source", "warehouse_allocations", "source_type IN ('own','partner_x','external')")
    op.create_check_constraint("ck_allocation_supply_status", "warehouse_allocations", "supply_status IN ('awaiting_receipt','received','confirmed','cancelled')")
    op.add_column("x_settlements", sa.Column("allocation_id", sa.Integer(), sa.ForeignKey("warehouse_allocations.id")))
    op.drop_constraint("x_settlements_order_item_id_key", "x_settlements", type_="unique")
    op.create_unique_constraint("uq_x_allocation", "x_settlements", ["allocation_id"])
    op.create_index("uq_x_legacy_item", "x_settlements", ["order_item_id"], unique=True,
                    postgresql_where=sa.text("allocation_id IS NULL"))


def downgrade():
    # Split quantities cannot be losslessly collapsed into the former unique row.
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM warehouse_allocations WHERE managed_source)")):
        raise RuntimeError("Cannot downgrade while managed source allocations exist")
    op.drop_index("uq_x_legacy_item", table_name="x_settlements")
    op.drop_column("x_settlements", "allocation_id")
    op.create_unique_constraint("x_settlements_order_item_id_key", "x_settlements", ["order_item_id"])
    op.drop_constraint("ck_allocation_source", "warehouse_allocations", type_="check")
    op.drop_constraint("ck_allocation_supply_status", "warehouse_allocations", type_="check")
    op.drop_index("uq_allocation_legacy_item", table_name="warehouse_allocations")
    for name in ("receipt_reference", "receipt_simulated", "buying_purchase_id", "supplier_id", "supply_status", "source_type", "managed_source"):
        op.drop_column("warehouse_allocations", name)
    op.create_unique_constraint("uq_allocation_shipment_item", "warehouse_allocations", ["shipment_id", "order_item_id"])
