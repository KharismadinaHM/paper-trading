"""insider flags

Taruhan besar berpola "insider wallet" beserta skor, alasan, dan hasil setelah resolve.

Revision ID: 0016_insider_flags
Revises: 0015_wallet_category_candidates
Create Date: 2026-10-03 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0016_insider_flags'
down_revision: Union[str, Sequence[str], None] = '0015_wallet_category_candidates'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # init_db() mungkin sudah membuat tabel ini saat startup
    if sa.inspect(op.get_bind()).has_table("insider_flags"):
        return
    op.create_table(
        'insider_flags',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('wallet', sa.String(length=42), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('condition_id', sa.String(length=80), nullable=False),
        sa.Column('outcome', sa.String(length=100), nullable=True),
        sa.Column('outcome_index', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=512), nullable=True),
        sa.Column('slug', sa.String(length=255), nullable=True),
        sa.Column('cash', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('shares', sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column('avg_price', sa.Numeric(precision=8, scale=4), nullable=False),
        sa.Column('score', sa.Integer(), nullable=False),
        sa.Column('reasons', sa.Text(), nullable=True),
        sa.Column('wallet_age_days', sa.Numeric(precision=10, scale=1), nullable=True),
        sa.Column('markets_traded', sa.Integer(), nullable=True),
        sa.Column('market_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_trade_ts', sa.BigInteger(), nullable=True),
        sa.Column('last_trade_ts', sa.BigInteger(), nullable=True),
        sa.Column('flagged_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('alerted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('result', sa.String(length=10), nullable=True),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('wallet', 'condition_id', 'outcome_index', name='uq_insider_flag'),
    )
    op.create_index('idx_insider_flags_flagged_at', 'insider_flags', ['flagged_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_insider_flags_flagged_at', table_name='insider_flags')
    op.drop_table('insider_flags')
