"""
Memastikan migrasi Alembic dapat dijalankan dari nol dan sinkron dengan model ORM.
Jika test ini gagal setelah mengubah models.py, buat migrasi baru:
    alembic revision --autogenerate -m "<deskripsi>"
"""
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app.core.config import settings
from app.paper_trading.models import Base

ROOT = Path(__file__).resolve().parent.parent


def test_migrations_upgrade_and_match_models(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrations.db'}"
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))

    command.upgrade(cfg, "head")

    engine = create_engine(url)
    with engine.connect() as conn:
        # SQLite tidak punya tipe UUID native, jadi perbandingan tipe dimatikan di sini.
        # Perbandingan lengkap (termasuk tipe) dijalankan CI dengan `alembic check` di PostgreSQL.
        ctx = MigrationContext.configure(conn, opts={"compare_type": False})
        diff = compare_metadata(ctx, Base.metadata)
    engine.dispose()
    assert diff == [], f"Model dan migrasi tidak sinkron: {diff}"

    command.downgrade(cfg, "base")
