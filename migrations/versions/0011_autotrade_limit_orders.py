"""autotrade limit orders

Limit order paper untuk strategi maker auto trader.

Revision ID: 0011_autotrade_limit_orders
Revises: 0010_autotrade
Create Date: 2026-09-30 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0011_autotrade_limit_orders'
down_revision: Union[str, Sequence[str], None] = '0010_autotrade'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # init_db() mungkin sudah membuat tabel ini saat startup
    if sa.inspect(op.get_bind()).has_table("autotrade_limit_orders"):
        return
    op.create_table(
        'autotrade_limit_orders',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('decision_key', sa.String(length=255), nullable=False),
        sa.Column('strategy', sa.String(length=50), nullable=False),
        sa.Column('market_id', sa.String(length=255), nullable=False),
        sa.Column('token_id', sa.String(length=100), nullable=False),
        sa.Column('side', sa.String(length=10), nullable=False),
        sa.Column('outcome', sa.String(length=20), nullable=True),
        sa.Column('label', sa.String(length=255), nullable=True),
        sa.Column('limit_price', sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column('size_usd', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('model_prob', sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('url', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('decision_key'),
    )
    op.create_index('idx_autotrade_limit_orders_status', 'autotrade_limit_orders', ['status'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_autotrade_limit_orders_status', table_name='autotrade_limit_orders')
    op.drop_table('autotrade_limit_orders')
