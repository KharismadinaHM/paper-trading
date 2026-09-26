import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, Numeric,
    String, UniqueConstraint, event
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class TradeSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    YES = "YES"
    NO = "NO"


class PaperTradeStatus(str, enum.Enum):
    OPEN = "OPEN"
    WON = "WON"
    LOST = "LOST"
    CANCELLED = "CANCELLED"
    # Posisi ditutup manual (Paper Sell) sebelum market resolve
    CLOSED = "CLOSED"


class PaperOrderStatus(str, enum.Enum):
    OPEN = "OPEN"
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class PaperAccount(Base):
    __tablename__ = "paper_accounts"
    
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    initial_balance: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0.0"))
    current_balance: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0.0"))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0.0"))
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0.0"))
    total_fees: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0.0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # 1-to-Many Relationships
    orders: Mapped[List["PaperOrder"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    positions: Mapped[List["PaperPosition"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    trades: Mapped[List["PaperTrade"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    balance_snapshots: Mapped[List["PaperBalanceSnapshot"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    cash_movements: Mapped[List["PaperCashMovement"]] = relationship(back_populates="account", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("initial_balance >= 0", name="chk_accounts_initial_balance"),
        CheckConstraint("current_balance >= 0", name="chk_accounts_current_balance"),
        CheckConstraint("total_fees >= 0", name="chk_accounts_total_fees"),
    )


class PaperOrder(Base):
    __tablename__ = "paper_orders"
    
    paper_order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False)
    market_id: Mapped[str] = mapped_column(String(255), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    side: Mapped[TradeSide] = mapped_column(Enum(TradeSide, name="trade_side", create_type=False), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    position_size: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    shares: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    status: Mapped[PaperOrderStatus] = mapped_column(Enum(PaperOrderStatus, name="paper_order_status", create_type=False), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    account: Mapped["PaperAccount"] = relationship(back_populates="orders")
    
    __table_args__ = (
        CheckConstraint("entry_price >= 0", name="chk_orders_entry_price"),
        CheckConstraint("position_size >= 0", name="chk_orders_position_size"),
        CheckConstraint("shares >= 0", name="chk_orders_shares"),
        Index("idx_paper_orders_account_id", "account_id"),
    )


class PaperPosition(Base):
    __tablename__ = "paper_positions"
    
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False)
    market_id: Mapped[str] = mapped_column(String(255), nullable=False)
    side: Mapped[TradeSide] = mapped_column(Enum(TradeSide, name="trade_side", create_type=False), nullable=False)
    shares: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    average_entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    position_size: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0.0"))
    market_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    strategy_version: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    account: Mapped["PaperAccount"] = relationship(back_populates="positions")
    
    __table_args__ = (
        UniqueConstraint("account_id", "market_id", "side", name="uq_positions_account_market_side"),
        CheckConstraint("shares >= 0", name="chk_positions_shares"),
        CheckConstraint("average_entry_price >= 0", name="chk_positions_avg_entry"),
        CheckConstraint("position_size >= 0", name="chk_positions_size"),
    )


class PaperTrade(Base):
    __tablename__ = "paper_trades"
    
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False)
    market_id: Mapped[str] = mapped_column(String(255), nullable=False)
    side: Mapped[TradeSide] = mapped_column(Enum(TradeSide, name="trade_side", create_type=False), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    position_size: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    shares: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    exit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    gross_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    fees: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0.0"))
    net_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    status: Mapped[PaperTradeStatus] = mapped_column(Enum(PaperTradeStatus, name="paper_trade_status", create_type=False), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    strategy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    market_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    
    account: Mapped["PaperAccount"] = relationship(back_populates="trades")
    
    __table_args__ = (
        CheckConstraint("entry_price >= 0", name="chk_trades_entry_price"),
        CheckConstraint("position_size >= 0", name="chk_trades_position_size"),
        CheckConstraint("shares >= 0", name="chk_trades_shares"),
        CheckConstraint("exit_price >= 0", name="chk_trades_exit_price"),
        CheckConstraint("fees >= 0", name="chk_trades_fees"),
        Index("idx_trades_account_strategy", "account_id", "strategy_version"),
        Index("idx_trades_status", "status"),
        Index("idx_trades_opened_at", "opened_at"),
    )


class PaperBalanceSnapshot(Base):
    __tablename__ = "paper_balance_snapshots"
    
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False)
    trade_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("paper_trades.id", ondelete="SET NULL"), nullable=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    account: Mapped["PaperAccount"] = relationship(back_populates="balance_snapshots")
    trade: Mapped[Optional["PaperTrade"]] = relationship()
    
    __table_args__ = (
        CheckConstraint("balance >= 0", name="chk_snapshots_balance"),
    )


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    market_id: Mapped[str] = mapped_column(String(255), nullable=False)
    market_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="open")
    is_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolution_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    price_yes: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    price_no: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    current_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, default="Weather")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_market_snapshots_market_id", "market_id"),
        Index("idx_market_snapshots_timestamp", "timestamp"),
        Index("idx_market_snapshots_status", "status"),
        Index("idx_market_snapshots_category", "category"),
        Index("idx_market_snapshots_market_ts", "market_id", "timestamp"),
    )


# Kolom data market yang sama di market_snapshots (histori) dan market_latest (harga terkini)
MARKET_DATA_FIELDS = (
    "market_id", "market_name", "status", "is_resolved", "resolution_time", "end_date",
    "price_yes", "price_no", "current_price", "category", "timestamp",
)


class MarketLatest(Base):
    """
    Satu baris per market: observasi TERBARU dari collector. Dipakai untuk semua pembacaan
    harga, cek stale, dan daftar market, sehingga tidak perlu memindai seluruh histori.
    `timestamp` = kapan market terakhir diamati; `last_snapshot_at` = kapan baris histori
    terakhir ditulis ke market_snapshots.
    """
    __tablename__ = "market_latest"

    market_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    market_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="open")
    is_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolution_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    price_yes: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    price_no: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    current_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, default="Weather")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_snapshot_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_market_latest_resolved", "is_resolved"),
        Index("idx_market_latest_resolution_time", "resolution_time"),
    )


def upsert_market_latest(connection, rows, history_written: bool) -> None:
    """
    Upsert observasi ke market_latest. Baris lama hanya ditimpa oleh observasi yang
    lebih baru (atau sama), sehingga urutan insert tidak berpengaruh.
    """
    rows = [dict(r) for r in rows]
    if not rows:
        return
    for r in rows:
        r["last_snapshot_at"] = r["timestamp"] if history_written else None
    dialect = connection.dialect.name
    insert_fn = postgresql.insert if dialect == "postgresql" else sqlite.insert
    stmt = insert_fn(MarketLatest.__table__).values(rows)
    update_cols = {f: stmt.excluded[f] for f in MARKET_DATA_FIELDS if f != "market_id"}
    if history_written:
        update_cols["last_snapshot_at"] = stmt.excluded.last_snapshot_at
    table = MarketLatest.__table__
    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c.market_id],
        set_=update_cols,
        where=stmt.excluded.timestamp >= table.c.timestamp,
    )
    connection.execute(stmt)


@event.listens_for(MarketSnapshot, "after_insert")
def _sync_market_latest(mapper, connection, target) -> None:
    """Setiap baris histori baru otomatis memperbarui market_latest."""
    row = {f: getattr(target, f) for f in MARKET_DATA_FIELDS}
    if row["timestamp"] is None:  # server_default belum dimuat kembali ke objek
        row["timestamp"] = datetime.now(timezone.utc)
    upsert_market_latest(connection, [row], history_written=True)


class PaperCashMovement(Base):
    """Ledger setoran/penarikan modal. Dipakai sebagai basis ROI (total modal disetor)."""
    __tablename__ = "paper_cash_movements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # DEPOSIT
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    account: Mapped["PaperAccount"] = relationship(back_populates="cash_movements")

    __table_args__ = (
        CheckConstraint("amount > 0", name="chk_cash_movements_amount"),
        Index("idx_cash_movements_account_id", "account_id"),
    )


class MarketResolution(Base):
    """Hasil akhir market yang sudah resolve (sumber kebenaran untuk settlement)."""
    __tablename__ = "market_resolutions"

    market_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    market_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    winning_outcome: Mapped[str] = mapped_column(String(20), nullable=False)  # YES / NO / INVALID
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

