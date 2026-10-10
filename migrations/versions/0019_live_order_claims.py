"""live order claims

Kolom auto-claim (redeem) kemenangan order live: hash/ID transaksi & waktu klaim.

Revision ID: 0019_live_order_claims
Revises: 0018_live_orders
Create Date: 2026-10-07 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0019_live_order_claims'
down_revision: Union[str, Sequence[str], None] = '0018_live_orders'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # init_db() mungkin sudah menambahkan kolom ini saat startup
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("live_orders")}
    if "claim_tx" not in columns:
        op.add_column('live_orders', sa.Column('claim_tx', sa.String(length=120), nullable=True))
    if "claimed_at" not in columns:
        op.add_column('live_orders', sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('live_orders', 'claimed_at')
    op.drop_column('live_orders', 'claim_tx')
