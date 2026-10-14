"""reversal watches

Pelacakan favorit market suhu ≥90¢: warning waspada berbalik, konfirmasi benar berbalik, hasil akhir.

Revision ID: 0014_reversal_watches
Revises: 0013_station_forecasts
Create Date: 2026-10-01 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0014_reversal_watches'
down_revision: Union[str, Sequence[str], None] = '0013_station_forecasts'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # init_db() mungkin sudah membuat tabel ini saat startup
    if sa.inspect(op.get_bind()).has_table("reversal_watches"):
        return
    op.create_table(
        'reversal_watches',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('city', sa.String(length=50), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('local_date', sa.String(length=10), nullable=False),
        sa.Column('fav_label', sa.String(length=50), nullable=False),
        sa.Column('fav_market_id', sa.String(length=255), nullable=False),
        sa.Column('peak_price', sa.Numeric(precision=8, scale=4), nullable=False),
        sa.Column('last_price', sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column('event_volume', sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column('brackets', sa.Text(), nullable=False),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('warned_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('warning', sa.Text(), nullable=True),
        sa.Column('flipped_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('flip_detail', sa.Text(), nullable=True),
        sa.Column('outcome', sa.String(length=10), nullable=True),
        sa.Column('winning_bracket', sa.String(length=50), nullable=True),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('city', 'kind', 'local_date', 'fav_label', name='uq_reversal_watch'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('reversal_watches')
