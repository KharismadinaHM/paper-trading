"""wallet alert cooldown

Kolom anti-spam alert wallet yang diikuti: waktu alert terakhir & jumlah market yang dilewati selama jeda.

Revision ID: 0017_wallet_alert_cooldown
Revises: 0016_insider_flags
Create Date: 2026-10-05 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0017_wallet_alert_cooldown'
down_revision: Union[str, Sequence[str], None] = '0016_insider_flags'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # init_db() mungkin sudah menambahkan kolom ini saat startup
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("tracked_wallets")}
    if "last_alert_at" not in columns:
        op.add_column('tracked_wallets', sa.Column('last_alert_at', sa.DateTime(timezone=True), nullable=True))
    if "alerts_skipped" not in columns:
        op.add_column('tracked_wallets', sa.Column('alerts_skipped', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('tracked_wallets', 'alerts_skipped')
    op.drop_column('tracked_wallets', 'last_alert_at')
