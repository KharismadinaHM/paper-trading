"""live orders

Order uang asli (Polymarket CLOB) dari auto trader: batas harga, hasil eksekusi, dan hasil akhir.

Revision ID: 0018_live_orders
Revises: 0017_wallet_alert_cooldown
Create Date: 2026-10-06 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0018_live_orders'
down_revision: Union[str, Sequence[str], None] = '0017_wallet_alert_cooldown'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # init_db() mungkin sudah membuat tabel ini saat startup
    if sa.inspect(op.get_bind()).has_table("live_orders"):
        return
    op.create_table(
        'live_orders',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('decision_key', sa.String(length=255), nullable=False),
        sa.Column('strategy', sa.String(length=50), nullable=False),
        sa.Column('market_id', sa.String(length=255), nullable=False),
        sa.Column('token_id', sa.String(length=100), nullable=False),
        sa.Column('outcome', sa.String(length=10), nullable=False),
        sa.Column('title', sa.String(length=512), nullable=True),
        sa.Column('usd', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('max_price', sa.Numeric(precision=8, scale=4), nullable=False),
        sa.Column('model_prob', sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('order_id', sa.String(length=120), nullable=True),
        sa.Column('shares', sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column('avg_price', sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column('spent', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('local_day', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('result', sa.String(length=10), nullable=True),
        sa.Column('pnl', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('decision_key'),
    )
    op.create_index('idx_live_orders_day', 'live_orders', ['local_day'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_live_orders_day', table_name='live_orders')
    op.drop_table('live_orders')
