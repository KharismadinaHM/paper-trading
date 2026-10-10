"""autotrade

Status runtime dan log keputusan auto paper trader.

Revision ID: 0010_autotrade
Revises: 0009_wallet_tracking
Create Date: 2026-09-30 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0010_autotrade'
down_revision: Union[str, Sequence[str], None] = '0009_wallet_tracking'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # init_db() mungkin sudah membuat tabel ini saat startup
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("autotrade_state"):
        op.create_table(
            'autotrade_state',
            sa.Column('key', sa.String(length=50), nullable=False),
            sa.Column('value', sa.Text(), nullable=True),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('key'),
        )
    if not inspector.has_table("autotrade_decisions"):
        op.create_table(
            'autotrade_decisions',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('decision_key', sa.String(length=255), nullable=False),
            sa.Column('strategy', sa.String(length=50), nullable=False),
            sa.Column('market_id', sa.String(length=255), nullable=False),
            sa.Column('label', sa.String(length=255), nullable=True),
            sa.Column('side', sa.String(length=10), nullable=False),
            sa.Column('model_prob', sa.Numeric(precision=8, scale=4), nullable=True),
            sa.Column('price', sa.Numeric(precision=10, scale=6), nullable=True),
            sa.Column('fee', sa.Numeric(precision=10, scale=6), nullable=True),
            sa.Column('edge', sa.Numeric(precision=8, scale=4), nullable=True),
            sa.Column('size_usd', sa.Numeric(precision=12, scale=2), nullable=True),
            sa.Column('status', sa.String(length=20), nullable=False),
            sa.Column('reason', sa.Text(), nullable=True),
            sa.Column('local_day', sa.String(length=10), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('decision_key'),
        )
        op.create_index('idx_autotrade_decisions_day', 'autotrade_decisions', ['local_day'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_autotrade_decisions_day', table_name='autotrade_decisions')
    op.drop_table('autotrade_decisions')
    op.drop_table('autotrade_state')
