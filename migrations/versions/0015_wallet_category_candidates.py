"""wallet category candidates

Rekomendasi wallet per kategori market (WEATHER, CRYPTO, SPORTS, …). Menggantikan wallet_candidates.

Revision ID: 0015_wallet_category_candidates
Revises: 0014_reversal_watches
Create Date: 2026-10-03 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0015_wallet_category_candidates'
down_revision: Union[str, Sequence[str], None] = '0014_reversal_watches'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("wallet_candidates"):
        op.drop_table('wallet_candidates')  # diganti tabel per kategori; isinya cache yang dibangun ulang
    # init_db() mungkin sudah membuat tabel ini saat startup
    if inspector.has_table("wallet_category_candidates"):
        return
    op.create_table(
        'wallet_category_candidates',
        sa.Column('category', sa.String(length=20), nullable=False),
        sa.Column('address', sa.String(length=42), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('rank', sa.Integer(), nullable=False),
        sa.Column('score', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('stats_json', sa.Text(), nullable=True),
        sa.Column('discovered_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('category', 'address'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('wallet_category_candidates')
    op.create_table(
        'wallet_candidates',
        sa.Column('address', sa.String(length=42), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('rank', sa.Integer(), nullable=False),
        sa.Column('score', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('stats_json', sa.Text(), nullable=True),
        sa.Column('discovered_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('address'),
    )
