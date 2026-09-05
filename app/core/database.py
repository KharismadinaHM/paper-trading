"""
Database session and engine management.
"""
from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from app.core.config import settings

# Engine configuration with connection pooling and pre-ping to handle stale connections
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

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


def init_db() -> None:
    """
    Membuat seluruh tabel database jika belum ada (paper_accounts, paper_positions, 
    paper_orders, paper_trades, market_snapshots, dll.).
    Aman dipanggil berulang kali (idempotent).
    """
    from app.paper_trading.models import Base
    Base.metadata.create_all(bind=engine)
