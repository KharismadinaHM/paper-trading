"""wallet tracking

Wallet Polymarket yang dilacak/diikuti, kandidat wallet menarik, dan log alert transaksi wallet.

Revision ID: 0009_wallet_tracking
Revises: 0008_station_readings
Create Date: 2026-09-30 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009_wallet_tracking'
down_revision: Union[str, Sequence[str], None] = '0008_station_readings'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # init_db() mungkin sudah membuat tabel ini saat startup
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("tracked_wallets"):
        op.create_table(
            'tracked_wallets',
            sa.Column('address', sa.String(length=42), nullable=False),
            sa.Column('name', sa.String(length=255), nullable=True),
            sa.Column('status', sa.String(length=20), nullable=False),
            sa.Column('follow', sa.Boolean(), nullable=False),
            sa.Column('source', sa.String(length=20), nullable=False),
            sa.Column('added_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('last_activity_ts', sa.BigInteger(), nullable=True),
            sa.Column('stats_json', sa.Text(), nullable=True),
            sa.Column('stats_at', sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint('address'),
        )
    if not inspector.has_table("wallet_candidates"):
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
    if not inspector.has_table("wallet_alert_log"):
        op.create_table(
            'wallet_alert_log',
            sa.Column('transaction_hash', sa.String(length=80), nullable=False),
            sa.Column('asset', sa.String(length=100), nullable=False),
            sa.Column('address', sa.String(length=42), nullable=False),
            sa.Column('timestamp', sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint('transaction_hash', 'asset'),
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('wallet_alert_log')
    op.drop_table('wallet_candidates')
    op.drop_table('tracked_wallets')
