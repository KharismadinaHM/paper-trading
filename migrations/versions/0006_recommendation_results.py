"""recommendation results

Hasil saran beli bot (WIN / LOSS / VOID, bracket pemenang) dan semua bracket event saat saran
dikirim, untuk statistik win rate.

Revision ID: 0006_recommendation_results
Revises: 0005_market_volume
Create Date: 2026-09-28 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006_recommendation_results'
down_revision: Union[str, Sequence[str], None] = '0005_market_volume'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_ALERT_COLUMNS = (
    ('bracket', sa.String(length=100)),
    ('result', sa.String(length=10)),
    ('winning_bracket', sa.String(length=100)),
    ('resolved_at', sa.DateTime(timezone=True)),
    ('checked_at', sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    """Upgrade schema."""
    # init_db() mungkin sudah membuat sebagian skema ini (migrasi ringan saat startup)
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("recommendation_alerts")}
    for name, type_ in NEW_ALERT_COLUMNS:
        if name not in columns:
            op.add_column('recommendation_alerts', sa.Column(name, type_, nullable=True))
    if not inspector.has_table("recommendation_alert_markets"):
        op.create_table(
            'recommendation_alert_markets',
            sa.Column('event_key', sa.String(length=255), nullable=False),
            sa.Column('market_id', sa.String(length=255), nullable=False),
            sa.Column('bracket', sa.String(length=100), nullable=True),
            sa.Column('rank', sa.Integer(), nullable=False),
            sa.Column('price_yes', sa.Numeric(precision=18, scale=6), nullable=True),
            sa.Column('winning_outcome', sa.String(length=20), nullable=True),
            sa.ForeignKeyConstraint(['event_key'], ['recommendation_alerts.event_key'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('event_key', 'market_id'),
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('recommendation_alert_markets')
    for name, _ in reversed(NEW_ALERT_COLUMNS):
        op.drop_column('recommendation_alerts', name)
