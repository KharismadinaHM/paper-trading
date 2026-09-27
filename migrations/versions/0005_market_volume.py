"""market volume

Volume trading kumulatif (USD) per market di market_latest, untuk memilih kota bervolume
terbesar pada notifikasi rekomendasi Telegram.

Revision ID: 0005_market_volume
Revises: 0004_recommendation_alerts
Create Date: 2026-09-27 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005_market_volume'
down_revision: Union[str, Sequence[str], None] = '0004_recommendation_alerts'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # init_db() mungkin sudah menambahkan kolom ini (migrasi ringan saat startup)
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("market_latest")}
    if "volume" not in columns:
        op.add_column('market_latest', sa.Column('volume', sa.Numeric(precision=20, scale=2), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('market_latest', 'volume')
