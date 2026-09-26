"""
Database session and engine management.
"""
from typing import Any, Dict, Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateColumn

from app.core.config import settings


def _engine_kwargs(url: str) -> Dict[str, Any]:
    """Parameter pool hanya valid untuk server database (PostgreSQL), bukan SQLite."""
    if url.startswith("sqlite"):
        return {}
    return {"pool_pre_ping": True, "pool_size": 10, "max_overflow": 20}


engine = create_engine(settings.DATABASE_URL, **_engine_kwargs(settings.DATABASE_URL))

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency yielding a database session.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_session() -> Session:
    """
    Provides a standalone Session instance for background workers or scripts.
    Caller should manage or close the session when finished.
    """
    return SessionLocal()


def _add_missing_columns(bind: Engine) -> None:
    """
    Migrasi ringan: menambahkan kolom nullable baru ke tabel yang sudah ada.
    `create_all` hanya membuat tabel baru dan tidak pernah mengubah tabel lama.
    Untuk perubahan skema yang lebih kompleks gunakan Alembic.
    """
    from app.paper_trading.models import Base

    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    with bind.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_cols or not column.nullable:
                    continue
                col_ddl = CreateColumn(column).compile(dialect=bind.dialect)
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col_ddl}"))


def _ensure_enum_values(bind: Engine) -> None:
    """Menambahkan nilai enum baru ke tipe ENUM native PostgreSQL yang sudah ada."""
    if bind.dialect.name != "postgresql":
        return
    from app.paper_trading.models import PaperTradeStatus

    with bind.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for status in PaperTradeStatus:
            conn.execute(text(f"ALTER TYPE paper_trade_status ADD VALUE IF NOT EXISTS '{status.value}'"))


def init_db(bind: Engine = None) -> None:
    """
    Membuat seluruh tabel database jika belum ada (paper_accounts, paper_positions,
    paper_orders, paper_trades, market_snapshots, dll.), lalu menerapkan migrasi ringan
    (kolom nullable baru, nilai enum baru, index baru) pada tabel yang sudah ada.
    Aman dipanggil berulang kali (idempotent).
    """
    from app.paper_trading.models import Base

    bind = bind or engine
    Base.metadata.create_all(bind=bind)
    _add_missing_columns(bind)
    _ensure_enum_values(bind)
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            index.create(bind=bind, checkfirst=True)
