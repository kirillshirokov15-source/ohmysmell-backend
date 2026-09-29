"""Bind procurement threads to the verified supplier mailbox, never customer Gmail."""
from alembic import op
import sqlalchemy as sa
revision = 'o48ca532ab65'
down_revision = 'n37b9421fa54'
branch_labels = depends_on = None


def upgrade():
    op.add_column('buying_purchases', sa.Column('supplier_mailbox_account', sa.String(320), nullable=True))
    # Existing unbound threads stay quarantined; no mailbox identity is invented.


def downgrade():
    raise RuntimeError('Mailbox binding is durable; restore a verified backup')
