"""Preserve channel contact details and enforce order money invariants."""
from alembic import op
import sqlalchemy as sa

revision = "i82c4f76ab09"
down_revision = "h71b3e65fa98"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("draft_orders", sa.Column("contact_details", sa.JSON(), nullable=False, server_default="{}"))
    op.alter_column("draft_orders", "contact_details", server_default=None)
    # NOT VALID protects new writes without guessing how to repair historical
    # production records. Validation is a separate reviewed migration gate.
    op.execute("ALTER TABLE orders ADD CONSTRAINT ck_orders_total_nonnegative CHECK (total >= 0) NOT VALID")
    op.execute("ALTER TABLE order_items ADD CONSTRAINT ck_order_items_money_quantity CHECK (qty > 0 AND price >= 0 AND item_total = price * qty) NOT VALID")


def downgrade():
    raise RuntimeError("Contact details must be retained; use reviewed forward fix or restore")
