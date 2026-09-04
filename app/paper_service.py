"""
Paper Service Interface and Stub Implementation.
Ganti implementasi fungsi di bawah ini dengan database query aktual saat wiring ke PostgreSQL/SQLAlchemy.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session, aliased

try:
    from app.core.config import settings
    from app.core.database import get_db_session
    from app.core.logging import get_logger
    from app.paper_trading.models import MarketSnapshot, PaperOrder, PaperOrderStatus, TradeSide
    from app.paper_trading.settlement_engine import (
        apply_slippage_and_spread,
        calculate_shares,
        evaluate_risk_and_rules,
    )
    from app.paper_trading.suggestions import filter_market_suggestions, search_markets
except ImportError:
    try:
        from core.config import settings
        from core.database import get_db_session
        from core.logging import get_logger
        from paper_trading.models import MarketSnapshot, PaperOrder, PaperOrderStatus, TradeSide
        from paper_trading.settlement_engine import (
            apply_slippage_and_spread,
            calculate_shares,
            evaluate_risk_and_rules,
        )
        from paper_trading.suggestions import filter_market_suggestions, search_markets
    except ImportError:
        from .core.config import settings
        from .core.database import get_db_session
        from .core.logging import get_logger
        from .paper_trading.models import MarketSnapshot, PaperOrder, PaperOrderStatus, TradeSide
        from .paper_trading.settlement_engine import (
            apply_slippage_and_spread,
            calculate_shares,
            evaluate_risk_and_rules,
        )
        from .paper_trading.suggestions import filter_market_suggestions, search_markets

logger = get_logger("paper_service")

# In-memory runtime state untuk tracking saldo dan pesanan paper trading
_account_state: Dict[str, Any] = {
    "balance": Decimal("20.00"),
    "invested": Decimal("3.00"),
    "realized_pnl": Decimal("0.42"),
    "win_rate": Decimal("0.80"),
    "open_trades": 3,
}
_paper_orders: List[Dict[str, Any]] = []


def start_paper_trading(strategy: Optional[str] = None) -> Dict[str, Any]:
    """
    Memulai engine paper trading (listener/collector/strategy loop).
    """
    return {
        "status": "running",
        "strategy": strategy or "all_active",
        "message": f"Paper trading engine started with strategy: {strategy or 'all'}"
    }


def get_account_status() -> Dict[str, Any]:
    """
    Mengambil ringkasan status paper account saat ini.
    """
    return dict(_account_state)


def get_open_positions() -> List[Dict[str, Any]]:
    """
    Mengambil daftar posisi yang sedang terbuka (open positions).
    """
    manual_positions = []
    for o in reversed(_paper_orders):
        if o.get("status") == "OPEN":
            curr_p = o.get("actual_price", o.get("entry_price", Decimal("0")))
            sz = o.get("position_size", Decimal("0"))
            sh = o.get("shares", Decimal("0"))
            manual_positions.append({
                "market": o.get("market_name", o.get("market_id")),
                "side": o.get("side", "BUY"),
                "entry_price": o.get("entry_price"),
                "size": sz,
                "shares": sh,
                "current_price": curr_p,
                "unrealized_pnl": (sh * curr_p) - sz,
                "strategy_version": o.get("strategy_version", "manual"),
            })

    base_positions = [
        {
            "market": "Will NYC exceed 85°F on Sep 5?",
            "side": "BUY",
            "entry_price": Decimal("0.65"),
            "size": Decimal("1.00"),
            "shares": Decimal("1.5385"),
            "current_price": Decimal("0.72"),
            "unrealized_pnl": Decimal("0.11"),
            "strategy_version": "weather_v1",
        },
        {
            "market": "Will Miami rain > 0.5 in on Sep 6?",
            "side": "BUY",
            "entry_price": Decimal("0.40"),
            "size": Decimal("1.00"),
            "shares": Decimal("2.5000"),
            "current_price": Decimal("0.38"),
            "unrealized_pnl": Decimal("-0.05"),
            "strategy_version": "weather_v1",
        },
        {
            "market": "Will Chicago wind > 25mph on Sep 5?",
            "side": "BUY",
            "entry_price": Decimal("0.55"),
            "size": Decimal("1.00"),
            "shares": Decimal("1.8182"),
            "current_price": Decimal("0.58"),
            "unrealized_pnl": Decimal("0.05"),
            "strategy_version": "weather_v2",
        },
    ]
    return manual_positions + base_positions


def get_trade_history(
    limit: int = 20, 
    strategy_version: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Mengambil riwayat trade yang telah closed/selesai.
    """
    all_trades = [
        {
            "id": "tr-001",
            "date": "2026-09-01 10:15",
            "market": "Austin high > 95°F Sep 1",
            "side": "BUY",
            "entry_price": Decimal("0.70"),
            "exit_price": Decimal("1.00"),
            "size": Decimal("1.00"),
            "status": "WON",
            "net_pnl": Decimal("0.43"),
            "strategy_version": "weather_v1",
        },
        {
            "id": "tr-002",
            "date": "2026-09-01 14:30",
            "market": "Seattle rain > 0.1 in Sep 1",
            "side": "BUY",
            "entry_price": Decimal("0.50"),
            "exit_price": Decimal("0.00"),
            "size": Decimal("1.00"),
            "status": "LOST",
            "net_pnl": Decimal("-1.00"),
            "strategy_version": "weather_v1",
        },
        {
            "id": "tr-003",
            "date": "2026-09-02 09:00",
            "market": "Dallas temp > 90°F Sep 2",
            "side": "BUY",
            "entry_price": Decimal("0.60"),
            "exit_price": Decimal("1.00"),
            "size": Decimal("1.00"),
            "status": "WON",
            "net_pnl": Decimal("0.67"),
            "strategy_version": "weather_v1",
        },
        {
            "id": "tr-004",
            "date": "2026-09-02 11:20",
            "market": "Denver snow > 1 in Sep 2",
            "side": "BUY",
            "entry_price": Decimal("0.75"),
            "exit_price": Decimal("1.00"),
            "size": Decimal("1.00"),
            "status": "WON",
            "net_pnl": Decimal("0.33"),
            "strategy_version": "weather_v2",
        },
    ]
    if strategy_version:
        all_trades = [t for t in all_trades if t.get("strategy_version") == strategy_version]
    return all_trades[:limit]


def get_performance(strategy_version: Optional[str] = None) -> Dict[str, Any]:
    """
    Mengambil metrik performa (Win Rate, ROI, Drawdown, Realized/Unrealized P/L).
    """
    if strategy_version == "weather_v2":
        return {
            "strategy_version": "weather_v2",
            "trades": 1,
            "wins": 1,
            "losses": 0,
            "win_rate": Decimal("1.00"),
            "roi": Decimal("0.0165"),
            "max_drawdown": Decimal("0.00"),
            "realized_pnl": Decimal("0.33"),
            "unrealized_pnl": Decimal("0.05"),
        }
    return {
        "strategy_version": strategy_version or "all",
        "trades": 4,
        "wins": 3,
        "losses": 1,
        "win_rate": Decimal("0.75"),
        "roi": Decimal("0.0210"),
        "max_drawdown": Decimal("0.0487"),  # ~4.87%
        "realized_pnl": Decimal("0.42"),
        "unrealized_pnl": Decimal("0.11"),
    }


def reset_paper_account() -> Dict[str, Any]:
    """
    Mereset seluruh paper account ke kondisi awal (saldo awal, hapus trades & positions).
    """
    _account_state["balance"] = Decimal("20.00")
    _account_state["invested"] = Decimal("0.00")
    _account_state["realized_pnl"] = Decimal("0.00")
    _account_state["win_rate"] = Decimal("0.00")
    _account_state["open_trades"] = 0
    _paper_orders.clear()
    return {
        "success": True,
        "initial_balance": Decimal("20.00"),
        "message": "Paper account reset to $20.00 initial balance. All trades wiped."
    }


def get_equity_snapshots() -> List[Dict[str, Any]]:
    """
    Mengambil data riwayat balance & equity dari waktu ke waktu untuk grafik Equity Curve.
    """
    return [
        {"timestamp": "2026-09-01 10:00", "balance": Decimal("20.00"), "equity": Decimal("20.00")},
        {"timestamp": "2026-09-01 10:15", "balance": Decimal("20.43"), "equity": Decimal("20.43")},
        {"timestamp": "2026-09-01 14:30", "balance": Decimal("19.43"), "equity": Decimal("19.43")},
        {"timestamp": "2026-09-02 09:00", "balance": Decimal("20.10"), "equity": Decimal("20.10")},
        {"timestamp": "2026-09-02 11:20", "balance": Decimal("20.43"), "equity": Decimal("20.43")},
        {"timestamp": "2026-09-03 12:00", "balance": Decimal("20.42"), "equity": Decimal("20.53")},
    ]


def _format_market_snapshot(
    snapshot: MarketSnapshot,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Memformat objek MarketSnapshot ORM menjadi dictionary dengan field yang konsisten,
    termasuk field 'timestamp' dan 'is_stale' (freshness check).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    snap_ts = snapshot.timestamp
    is_stale = False
    if snap_ts is not None:
        ts_aware = snap_ts if snap_ts.tzinfo is not None else snap_ts.replace(tzinfo=timezone.utc)
        now_aware = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        age_seconds = (now_aware - ts_aware).total_seconds()
        # Threshold staleness: 15 menit (3x interval collector 300 detik)
        stale_threshold = float(getattr(settings, "COLLECTOR_INTERVAL_SECONDS", 300) * 3)
        is_stale = age_seconds > stale_threshold

    price_yes = Decimal(str(snapshot.price_yes)) if snapshot.price_yes is not None else None
    price_no = Decimal(str(snapshot.price_no)) if snapshot.price_no is not None else None
    current_price = (
        Decimal(str(snapshot.current_price))
        if snapshot.current_price is not None
        else (price_yes if price_yes is not None else price_no)
    )

    res_time = snapshot.resolution_time or snapshot.end_date

    return {
        "market_id": str(snapshot.market_id),
        "market_name": str(snapshot.market_name),
        "category": str(snapshot.category or "Weather"),
        "status": str(snapshot.status or "open"),
        "is_resolved": bool(snapshot.is_resolved),
        "resolution_time": res_time,
        "end_date": snapshot.end_date or res_time,
        "price_yes": price_yes,
        "price_no": price_no,
        "current_price": current_price,
        "timestamp": snap_ts,
        "is_stale": is_stale,
    }


def get_market_snapshots(
    now: Optional[datetime] = None,
    include_resolved: bool = False,
    db: Optional[Session] = None,
) -> List[Dict[str, Any]]:
    """
    Mengambil snapshot pasar terbaru per market_id langsung dari database PostgreSQL.
    Mengambil HANYA snapshot terbaru per market_id menggunakan pendekatan:
        SELECT DISTINCT ON (market_id) * FROM market_snapshots ORDER BY market_id, timestamp DESC
    Secara default mengecualikan market yang sudah resolved (is_resolved=True) kecuali include_resolved=True.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    close_session = False
    if db is None:
        try:
            db = get_db_session()
            close_session = True
        except Exception as conn_err:
            logger.error(f"Gagal membuka koneksi database untuk market_snapshots: {conn_err}")
            return []

    try:
        bind = db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            # PostgreSQL native DISTINCT ON (market_id)
            subq = (
                db.query(MarketSnapshot)
                .distinct(MarketSnapshot.market_id)
                .order_by(MarketSnapshot.market_id, MarketSnapshot.timestamp.desc())
                .subquery()
            )
            SnapshotAlias = aliased(MarketSnapshot, subq)
            query = db.query(SnapshotAlias)
            if not include_resolved:
                query = query.filter(
                    SnapshotAlias.is_resolved.is_(False),
                    func.lower(SnapshotAlias.status) != "resolved",
                )
            snapshots = query.all()
        else:
            # Standar ANSI SQL Window Function (kompatibel SQLite & dialect lain untuk test/fixture)
            subq = (
                db.query(
                    MarketSnapshot.id.label("sid"),
                    func.row_number().over(
                        partition_by=MarketSnapshot.market_id,
                        order_by=MarketSnapshot.timestamp.desc(),
                    ).label("rn"),
                ).subquery()
            )
            query = (
                db.query(MarketSnapshot)
                .join(subq, MarketSnapshot.id == subq.c.sid)
                .filter(subq.c.rn == 1)
            )
            if not include_resolved:
                query = query.filter(
                    MarketSnapshot.is_resolved.is_(False),
                    func.lower(MarketSnapshot.status) != "resolved",
                )
            snapshots = query.all()

        return [_format_market_snapshot(s, now=now) for s in snapshots]
    except Exception as e:
        logger.error(f"Error mengambil market snapshots dari database: {e}", exc_info=True)
        return []
    finally:
        if close_session and db is not None:
            db.close()


def get_market_suggestions(
    max_hours_to_resolution: float = 6.0,
    min_price: float = 0.70,
    max_price: float = 0.75,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Mengambil saran market dari data Market Collector yang memenuhi kriteria filter.
    """
    raw_markets = get_market_snapshots(now=now, include_resolved=False)
    return filter_market_suggestions(
        markets=raw_markets,
        max_hours_to_resolution=max_hours_to_resolution,
        min_price=min_price,
        max_price=max_price,
        now=now,
    )


def search_market_snapshots(
    query: str = "",
    category: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Mencari market berdasarkan keyword, kategori, dan rentang harga dari data Market Collector.
    """
    raw_markets = get_market_snapshots(now=now, include_resolved=False)
    return search_markets(
        markets=raw_markets,
        query=query,
        category=category,
        min_price=min_price,
        max_price=max_price,
        now=now,
    )


def get_market_by_id(
    market_id: str,
    now: Optional[datetime] = None,
    db: Optional[Session] = None,
) -> Optional[Dict[str, Any]]:
    """
    Mengambil data snapshot pasar real-time TERBARU langsung dari tabel market_snapshots berdasarkan market_id:
        SELECT * FROM market_snapshots WHERE market_id = :market_id ORDER BY timestamp DESC LIMIT 1
    Jika tidak ada snapshot, mengembalikan None.
    """
    if not market_id:
        return None

    if now is None:
        now = datetime.now(timezone.utc)

    close_session = False
    if db is None:
        try:
            db = get_db_session()
            close_session = True
        except Exception as conn_err:
            logger.error(f"Gagal membuka koneksi database untuk get_market_by_id: {conn_err}")
            return None

    try:
        snapshot = (
            db.query(MarketSnapshot)
            .filter(MarketSnapshot.market_id == str(market_id))
            .order_by(MarketSnapshot.timestamp.desc())
            .first()
        )
        if snapshot is None:
            return None

        return _format_market_snapshot(snapshot, now=now)
    except Exception as e:
        logger.error(f"Error query snapshot market_id='{market_id}': {e}", exc_info=True)
        return None
    finally:
        if close_session and db is not None:
            db.close()


def get_paper_orders(limit: int = 50) -> List[Dict[str, Any]]:
    """
    Mengambil daftar order paper trading yang telah tersimpan.
    """
    return list(reversed(_paper_orders))[:limit]


def create_paper_order(
    market_id: str,
    side: str,
    position_size: Decimal,
    user_viewed_price: Optional[Decimal] = None,
    strategy_version: str = "manual",
    db_session: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Membuat paper order manual dengan proteksi Anti Stale Price.
    
    Urutan proses WAJIB:
    1. Fetch harga real-time terbaru dari Market Collector untuk market_id ini.
       (Reject jika market tidak ditemukan, sudah resolved, atau harga snapshot tidak tersedia).
    2. Hitung selisih harga dengan harga yang dilihat user (HANYA untuk generate warning jika > threshold).
       JANGAN gunakan user_viewed_price untuk kalkulasi settlement/shares/slippage apapun!
    3. apply_slippage_and_spread(historical_mid_price=real_time_price, ...) -> execution_price.
    4. evaluate_risk_and_rules(position_size, available_balance, max_position_size, ...)
       -> JIKA REJECTED: langsung hentikan proses (raise ValueError), JANGAN lanjut ke kalkulasi shares
          atau simpan ke database!
    5. calculate_shares(position_size, execution_price) -> shares.
    6. Simpan ke tabel paper_orders (status OPEN) dengan entry_price = execution_price (hasil real-time).
    """
    if position_size <= Decimal("0"):
        raise ValueError("Position size must be strictly positive")

    # 1. Fetch harga real-time terbaru langsung dari Market Collector
    market = get_market_by_id(market_id, now=now)
    if not market:
        raise ValueError(f"Market '{market_id}' tidak ditemukan di data Market Collector.")

    if market.get("is_resolved") or str(market.get("status", "")).lower() == "resolved":
        raise ValueError(f"Market '{market_id}' sudah resolved dan tidak dapat menerima order.")

    normalized_side = str(side).strip().upper()
    if normalized_side in ["YES", "BUY"]:
        raw_price = market.get("price_yes") if market.get("price_yes") is not None else market.get("current_price")
    elif normalized_side in ["NO", "SELL"]:
        raw_price = market.get("price_no")
        if raw_price is None and market.get("price_yes") is not None:
            raw_price = Decimal("1.0") - Decimal(str(market.get("price_yes")))
    else:
        raise ValueError(f"Side '{side}' tidak valid. Gunakan 'YES' atau 'NO'.")

    if raw_price is None or Decimal(str(raw_price)) <= Decimal("0") or Decimal(str(raw_price)) >= Decimal("1"):
        raise ValueError(f"Data harga real-time tidak tersedia atau tidak valid untuk market '{market_id}'.")

    # HARGA REAL-TIME TERBARU
    real_time_price = Decimal(str(raw_price))

    # 2. Audit Trail & Anti Stale Price Warning
    # CATATAN KRUSIAL: user_viewed_price HANYA digunakan untuk logging/audit trail dan memicu warning.
    # Nilai ini TIDAK PERNAH dilempar ke kalkulasi slippage, risk, ataupun shares!
    warning_message: Optional[str] = None
    if user_viewed_price is not None:
        user_p = Decimal(str(user_viewed_price))
        if user_p > Decimal("0"):
            price_divergence = abs(real_time_price - user_p) / user_p
            threshold = Decimal(str(getattr(settings, "PRICE_DIVERGENCE_WARNING_THRESHOLD", "0.05")))
            if price_divergence > threshold:
                divergence_pct = (price_divergence * Decimal("100")).quantize(Decimal("0.1"))
                warning_message = (
                    f"Warning: Terjadi pergerakan harga pasar sebesar {divergence_pct}% "
                    f"dari ${user_p:.2f} (yang dilihat user saat klik buy) "
                    f"menjadi ${real_time_price:.2f} (harga real-time saat eksekusi)."
                )
                logger.warning(
                    f"Anti-Stale Warning: market={market_id}, viewed=${user_p:.2f}, "
                    f"real_time=${real_time_price:.2f}, diff={divergence_pct}% > threshold={threshold * 100}%"
                )

    # Freshness / Staleness check
    if market.get("is_stale"):
        ts_val = market.get("timestamp")
        ts_str = ts_val.strftime("%Y-%m-%d %H:%M:%S UTC") if hasattr(ts_val, "strftime") else str(ts_val)
        stale_msg = f"Perhatian: Data harga pasar ini terakhir diperbarui pada {ts_str} (berstatus stale > 15 menit)."
        warning_message = f"{warning_message} | {stale_msg}" if warning_message else stale_msg
        logger.warning(f"Anti-Stale Notice: Market '{market_id}' menggunakan data snapshot stale (ts: {ts_str})")

    # 3. apply_slippage_and_spread menggunakan harga real-time
    spread_bps = int(getattr(settings, "SPREAD_BPS", 0))
    slippage_bps = int(getattr(settings, "SLIPPAGE_BPS", 0))
    execution_price = apply_slippage_and_spread(
        historical_mid_price=real_time_price,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        is_buy=True,
    )

    # 4. evaluate_risk_and_rules
    current_status = get_account_status()
    available_balance = Decimal(str(current_status.get("balance", "20.00")))
    max_pos_size = Decimal(str(getattr(settings, "MAX_POSITION_SIZE", "1.00")))

    is_approved, rejection_reason = evaluate_risk_and_rules(
        position_size=position_size,
        available_balance=available_balance,
        max_position_size=max_pos_size,
        historical_price_available=True,
    )

    if not is_approved:
        logger.warning(f"Paper order rejected by risk control: {rejection_reason}")
        # REJECTED -> Langsung raise error, JANGAN lanjut dan TIDAK ADA partial write ke DB!
        raise ValueError(rejection_reason)

    # 5. calculate_shares dari harga setelah slippage
    shares = calculate_shares(
        position_size=position_size,
        entry_price=execution_price,
    )

    # 6. Simpan ke tabel paper_orders (status OPEN)
    # Entry price yang disimpan adalah execution_price (berdasarkan real_time_price), BUKAN user_viewed_price!
    now_ts = now or datetime.now(timezone.utc)
    order_uuid = uuid.uuid4()

    if db_session is not None:
        try:
            side_enum = TradeSide.BUY
            if normalized_side == "SELL":
                side_enum = TradeSide.SELL
            elif hasattr(TradeSide, normalized_side):
                side_enum = getattr(TradeSide, normalized_side)

            order_db = PaperOrder(
                paper_order_id=order_uuid,
                account_id=getattr(db_session, "account_id", uuid.uuid4()),
                market_id=market_id,
                timestamp=now_ts,
                side=side_enum,
                entry_price=execution_price,
                position_size=position_size,
                shares=shares,
                status=PaperOrderStatus.OPEN,
                strategy_version=strategy_version,
            )
            db_session.add(order_db)
            db_session.commit()
        except Exception as e:
            logger.error(f"Failed to persist PaperOrder to database: {e}")
            if hasattr(db_session, "rollback"):
                db_session.rollback()
            raise

    # Simpan ke runtime/in-memory repository
    order_data = {
        "order_id": str(order_uuid),
        "market_id": market_id,
        "market_name": market.get("market_name", market_id),
        "side": normalized_side,
        "status": "OPEN",
        "requested_price": Decimal(str(user_viewed_price)) if user_viewed_price is not None else None,
        "actual_price": real_time_price,
        "execution_price": execution_price,
        "entry_price": execution_price,
        "position_size": position_size,
        "shares": shares,
        "strategy_version": strategy_version,
        "timestamp": now_ts.isoformat(),
        "warning": warning_message,
    }
    _paper_orders.append(order_data)

    # Update account balance in-memory
    _account_state["balance"] = max(Decimal("0.00"), _account_state["balance"] - position_size)
    _account_state["invested"] = _account_state["invested"] + position_size
    _account_state["open_trades"] = _account_state["open_trades"] + 1

    logger.info(
        f"Paper order successfully created: id={order_uuid}, market={market_id}, side={normalized_side}, "
        f"entry_price=${execution_price:.4f}, shares={shares}, status=OPEN"
    )

    return order_data



