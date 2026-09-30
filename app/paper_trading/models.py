import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, Numeric,
    String, Text, UniqueConstraint, event
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
    # Nama asli outcome yang dipetakan ke sisi YES / NO (mis. "Yes"/"No" atau "Up"/"Down")
    outcome_yes_label: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    outcome_no_label: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
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
    "price_yes", "price_no", "current_price", "category", "outcome_yes_label", "outcome_no_label",
    "timestamp",
)

# Kolom yang hanya ada di market_latest (tidak ikut histori market_snapshots)
LATEST_ONLY_FIELDS = ("volume", "yes_token_id", "resolution_station")


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
    outcome_yes_label: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    outcome_no_label: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # Volume trading kumulatif (USD) dari Gamma. Hanya disimpan di sini, bukan di histori:
    # volume berubah hampir setiap siklus dan akan membatalkan penulisan histori hemat.
    volume: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True)
    # Token CLOB outcome YES (untuk order book) & stasiun resolusi (ICAO / 'HKO') market suhu
    yes_token_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    resolution_station: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
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
    table = MarketLatest.__table__
    for f in LATEST_ONLY_FIELDS:
        if any(f in r for r in rows):
            # Baris tanpa nilai (mis. dari listener histori) tidak menghapus nilai lama
            update_cols[f] = func.coalesce(stmt.excluded[f], table.c[f])
    if history_written:
        update_cols["last_snapshot_at"] = stmt.excluded.last_snapshot_at
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


class RecommendationAlert(Base):
    """Event rekomendasi yang sudah dikirim ke Telegram (mencegah notifikasi ganda)."""
    __tablename__ = "recommendation_alerts"

    event_key: Mapped[str] = mapped_column(String(255), primary_key=True)  # kota|jenis|tanggal
    city: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    local_date: Mapped[str] = mapped_column(String(10), nullable=False)
    market_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    price_yes: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Hasil saran (diisi tracker setelah market resolve): WIN / LOSS / VOID; None = belum ada hasil
    bracket: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    result: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    winning_bracket: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # resolved_at = pelacakan hasil selesai; checked_at = terakhir dicek ke Gamma (throttle)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class RecommendationAlertMarket(Base):
    """Semua bracket sebuah event saat saran dikirim (rank 0 = saran utama), untuk melihat pemenangnya."""
    __tablename__ = "recommendation_alert_markets"

    event_key: Mapped[str] = mapped_column(
        ForeignKey("recommendation_alerts.event_key", ondelete="CASCADE"), primary_key=True)
    market_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    bracket: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    price_yes: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    winning_outcome: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # YES / NO / INVALID


class StationReading(Base):
    """Bacaan suhu stasiun real-time (HKO per 10 menit) untuk mendeteksi lonjakan suhu."""
    __tablename__ = "station_readings"

    station: Mapped[str] = mapped_column(String(20), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    temp: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    max_since_midnight: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    min_since_midnight: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)


class StationAlert(Base):
    """Alert lonjakan suhu yang sudah dikirim (throttle & dedupe per derajat per hari)."""
    __tablename__ = "station_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    station: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # spike / degree
    local_date: Mapped[str] = mapped_column(String(10), nullable=False)
    value: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("idx_station_alerts_station_date", "station", "local_date"),)


class TrackedWallet(Base):
    """
    Wallet Polymarket yang dilacak. status: 'tracking' (dipantau) / 'skipped' (disembunyikan dari
    rekomendasi). follow=True → alert Telegram real-time setiap wallet ini bertransaksi.
    """
    __tablename__ = "tracked_wallets"

    address: Mapped[str] = mapped_column(String(42), primary_key=True)  # lowercase 0x…
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="tracking")
    follow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")  # manual / discover
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_activity_ts: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # kursor alert (unix)
    stats_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stats_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class WalletCandidate(Base):
    """Hasil pencarian wallet menarik (leaderboard + statistik), diperbarui berkala."""
    __tablename__ = "wallet_candidates"

    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stats_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WalletAlertLog(Base):
    """Transaksi wallet yang sudah dialertkan (dedupe)."""
    __tablename__ = "wallet_alert_log"

    transaction_hash: Mapped[str] = mapped_column(String(80), primary_key=True)
    asset: Mapped[str] = mapped_column(String(100), primary_key=True)
    address: Mapped[str] = mapped_column(String(42), nullable=False)
    timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False)


class AutotradeState(Base):
    """Status runtime auto trader (mis. enabled on/off lewat /startbot /stopbot, laporan terakhir)."""
    __tablename__ = "autotrade_state"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AutotradeDecision(Base):
    """
    Keputusan auto trader yang dieksekusi (filled) atau ditolak risk engine (rejected).
    decision_key unik per strategi + market/event → paling banyak satu entri per market.
    """
    __tablename__ = "autotrade_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    strategy: Mapped[str] = mapped_column(String(50), nullable=False)
    market_id: Mapped[str] = mapped_column(String(255), nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    model_prob: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), nullable=True)
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)   # VWAP ask
    fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)     # biaya taker per share
    edge: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), nullable=True)
    size_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)                   # filled / rejected
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    local_day: Mapped[str] = mapped_column(String(10), nullable=False)                # hari WIB
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("idx_autotrade_decisions_day", "local_day"),)
