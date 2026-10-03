"""market token & station

Token CLOB outcome YES (untuk order book) dan stasiun resolusi market suhu di market_latest.

Revision ID: 0007_market_token_station
Revises: 0006_recommendation_results
Create Date: 2026-09-28 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007_market_token_station'
down_revision: Union[str, Sequence[str], None] = '0006_recommendation_results'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_COLUMNS = (
    ('yes_token_id', sa.String(length=100)),
    ('resolution_station', sa.String(length=20)),
)


def upgrade() -> None:
    """Upgrade schema."""
    # init_db() mungkin sudah menambahkan kolom ini (migrasi ringan saat startup)
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("market_latest")}
    for name, type_ in NEW_COLUMNS:
        if name not in columns:
            op.add_column('market_latest', sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    for name, _ in reversed(NEW_COLUMNS):
        op.drop_column('market_latest', name)
