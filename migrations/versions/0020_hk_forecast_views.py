"""hk forecast views

Pandangan peluang per bracket suhu max/min Hong Kong dari AI (Gemini), model bot HK, dan pasar, untuk
dinilai akurasinya setelah hari selesai.

Revision ID: 0020_hk_forecast_views
Revises: 0019_live_order_claims
Create Date: 2026-10-09 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0020_hk_forecast_views'
down_revision: Union[str, Sequence[str], None] = '0019_live_order_claims'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # init_db() mungkin sudah membuat tabel ini saat startup
    if sa.inspect(op.get_bind()).has_table("hk_forecast_views"):
        return
    op.create_table(
        'hk_forecast_views',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('local_date', sa.String(length=10), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('source', sa.String(length=10), nullable=False),
        sa.Column('probs', sa.Text(), nullable=False),
        sa.Column('point', sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_hk_forecast_views_date', 'hk_forecast_views', ['local_date', 'kind', 'source'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_hk_forecast_views_date', table_name='hk_forecast_views')
    op.drop_table('hk_forecast_views')
