"""station forecasts

Prediksi suhu per jam (dibuat sebelum jamnya) untuk tabel expected vs real Hong Kong.

Revision ID: 0013_station_forecasts
Revises: 0012_autotrade_signals
Create Date: 2026-10-01 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0013_station_forecasts'
down_revision: Union[str, Sequence[str], None] = '0012_autotrade_signals'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # init_db() mungkin sudah membuat tabel ini saat startup
    if sa.inspect(op.get_bind()).has_table("station_forecasts"):
        return
    op.create_table(
        'station_forecasts',
        sa.Column('station', sa.String(length=20), nullable=False),
        sa.Column('target_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('made_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('value', sa.Numeric(precision=6, scale=2), nullable=False),
        sa.PrimaryKeyConstraint('station', 'target_at', 'made_at'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('station_forecasts')
