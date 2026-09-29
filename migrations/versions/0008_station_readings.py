"""station readings

Bacaan suhu real-time stasiun (HKO per 10 menit) dan log alert lonjakan suhu.

Revision ID: 0008_station_readings
Revises: 0007_market_token_station
Create Date: 2026-09-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008_station_readings'
down_revision: Union[str, Sequence[str], None] = '0007_market_token_station'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # init_db() mungkin sudah membuat tabel ini saat startup
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("station_readings"):
        op.create_table(
            'station_readings',
            sa.Column('station', sa.String(length=20), nullable=False),
            sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('temp', sa.Numeric(precision=6, scale=2), nullable=False),
            sa.Column('max_since_midnight', sa.Numeric(precision=6, scale=2), nullable=True),
            sa.Column('min_since_midnight', sa.Numeric(precision=6, scale=2), nullable=True),
            sa.PrimaryKeyConstraint('station', 'observed_at'),
        )
    if not inspector.has_table("station_alerts"):
        op.create_table(
            'station_alerts',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('station', sa.String(length=20), nullable=False),
            sa.Column('kind', sa.String(length=20), nullable=False),
            sa.Column('local_date', sa.String(length=10), nullable=False),
            sa.Column('value', sa.Numeric(precision=6, scale=2), nullable=True),
            sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('idx_station_alerts_station_date', 'station_alerts', ['station', 'local_date'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_station_alerts_station_date', table_name='station_alerts')
    op.drop_table('station_alerts')
    op.drop_table('station_readings')
