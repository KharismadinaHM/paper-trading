"""autotrade signals

Sampel sinyal auto trader (ditrade & dilewati) beserta hasilnya untuk riset, dan kolom features
(konteks keputusan) di autotrade_decisions.

Revision ID: 0012_autotrade_signals
Revises: 0011_autotrade_limit_orders
Create Date: 2026-10-01 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0012_autotrade_signals'
down_revision: Union[str, Sequence[str], None] = '0011_autotrade_limit_orders'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # init_db() mungkin sudah membuat sebagian skema ini saat startup
    inspector = sa.inspect(op.get_bind())
    if "features" not in {c["name"] for c in inspector.get_columns("autotrade_decisions")}:
        op.add_column('autotrade_decisions', sa.Column('features', sa.Text(), nullable=True))
    if inspector.has_table("autotrade_signals"):
        return
    op.create_table(
        'autotrade_signals',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('signal_key', sa.String(length=255), nullable=False),
        sa.Column('strategy', sa.String(length=50), nullable=False),
        sa.Column('market_id', sa.String(length=255), nullable=False),
        sa.Column('label', sa.String(length=255), nullable=True),
        sa.Column('side', sa.String(length=10), nullable=False),
        sa.Column('model_prob', sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column('price', sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column('fee', sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column('edge', sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column('action', sa.String(length=10), nullable=False),
        sa.Column('skip_reason', sa.String(length=100), nullable=True),
        sa.Column('features', sa.Text(), nullable=True),
        sa.Column('local_day', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('outcome', sa.String(length=10), nullable=True),
        sa.Column('pnl_per_share', sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('signal_key'),
    )
    op.create_index('idx_autotrade_signals_strategy_created', 'autotrade_signals', ['strategy', 'created_at'])
    op.create_index('idx_autotrade_signals_pending', 'autotrade_signals', ['outcome', 'created_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_autotrade_signals_pending', table_name='autotrade_signals')
    op.drop_index('idx_autotrade_signals_strategy_created', table_name='autotrade_signals')
    op.drop_table('autotrade_signals')
    op.drop_column('autotrade_decisions', 'features')
