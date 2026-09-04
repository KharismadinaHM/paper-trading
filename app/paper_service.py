"""
Paper Service Interface and Stub Implementation.
Ganti implementasi fungsi di bawah ini dengan database query aktual saat wiring ke PostgreSQL/SQLAlchemy.
"""
import urllib.parse
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
_INITIAL_BALANCE = Decimal("20.00")

_account_state: Dict[str, Any] = {
    "balance": Decimal("20.00"),
    "initial_balance": Decimal("20.00"),
    "invested": Decimal("4.00"),
    "realized_pnl": Decimal("2.87"),
    "win_rate": Decimal("0.75"),
    "open_trades": 3,
    "deposit_total": Decimal("20.00"),
}
_paper_orders: List[Dict[str, Any]] = []
_paper_positions: Dict[str, Dict[str, Any]] = {}
_trade_history: List[Dict[str, Any]] = []


def _init_default_data():
    """Inisialisasi posisi dan riwayat awal sesuai gaya platform Polymarket."""
    _paper_positions.clear()
    _paper_positions["mkt-hk-temp_NO"] = {
        "id": "pos-001",
        "market_id": "mkt-hk-temp",
        "market_name": "Will the highest temperature in Hong Kong be 34°C or above on Sep 5?",
        "side": "NO",
        "shares": Decimal("2.84"),
        "average_entry_price": Decimal("0.703"),
        "position_size": Decimal("2.00"),
        "current_price": Decimal("0.79"),
        "strategy_version": "weather_v1",
        "created_at": datetime.now(timezone.utc) - timedelta(hours=8),
    }
    _paper_positions["mkt-seoul-temp_YES"] = {
        "id": "pos-002",
        "market_id": "mkt-seoul-temp",
        "market_name": "Will the highest temperature in Seoul (Incheon) be 30°C or above on Sep 5?",
        "side": "YES",
        "shares": Decimal("1.67"),
        "average_entry_price": Decimal("0.60"),
        "position_size": Decimal("1.00"),
        "current_price": Decimal("0.805"),
        "strategy_version": "weather_v1",
        "created_at": datetime.now(timezone.utc) - timedelta(hours=5),
    }
    _paper_positions["mkt-sg-temp_NO"] = {
        "id": "pos-003",
        "market_id": "mkt-sg-temp",
        "market_name": "Will the highest temperature in Singapore be 32°C or above on Sep 5?",
        "side": "NO",
        "shares": Decimal("1.28"),
        "average_entry_price": Decimal("0.78"),
        "position_size": Decimal("1.00"),
        "current_price": Decimal("0.715"),
        "strategy_version": "weather_v2",
        "created_at": datetime.now(timezone.utc) - timedelta(hours=3),
    }

    _trade_history.clear()
    _trade_history.extend([
        {
            "id": "tr-001",
            "date": "2026-09-01 10:15",
            "market": "Austin high > 95°F Sep 1",
            "market_id": "mkt-austin",
            "side": "YES",
            "entry_price": Decimal("0.70"),
            "exit_price": Decimal("1.00"),
            "size": Decimal("1.00"),
            "shares": Decimal("1.43"),
            "status": "WON",
            "net_pnl": Decimal("0.43"),
            "strategy_version": "weather_v1",
        },
        {
            "id": "tr-002",
            "date": "2026-09-01 14:30",
            "market": "Seattle rain > 0.1 in Sep 1",
            "market_id": "mkt-seattle",
            "side": "YES",
            "entry_price": Decimal("0.50"),
            "exit_price": Decimal("0.00"),
            "size": Decimal("1.00"),
            "shares": Decimal("2.00"),
            "status": "LOST",
            "net_pnl": Decimal("-1.00"),
            "strategy_version": "weather_v1",
        },
        {
            "id": "tr-003",
            "date": "2026-09-02 09:00",
            "market": "Dallas temp > 90°F Sep 2",
            "market_id": "mkt-dallas",
            "side": "YES",
            "entry_price": Decimal("0.60"),
            "exit_price": Decimal("1.00"),
            "size": Decimal("1.00"),
            "shares": Decimal("1.67"),
            "status": "WON",
            "net_pnl": Decimal("0.67"),
            "strategy_version": "weather_v1",
        },
        {
            "id": "tr-004",
            "date": "2026-09-02 11:20",
            "market": "Denver snow > 1 in Sep 2",
            "market_id": "mkt-denver",
            "side": "YES",
            "entry_price": Decimal("0.75"),
            "exit_price": Decimal("1.00"),
            "size": Decimal("1.00"),
            "shares": Decimal("1.33"),
            "status": "WON",
            "net_pnl": Decimal("0.33"),
            "strategy_version": "weather_v2",
        },
    ])


_init_default_data()


def start_paper_trading(strategy: Optional[str] = None) -> Dict[str, Any]:
    """
    Memulai engine paper trading (listener/collector/strategy loop).
    """
    return {
        "status": "running",
        "strategy": strategy or "all_active",
        "message": f"Paper trading engine started with strategy: {strategy or 'all'}"
    }


def get_polymarket_url(market_id: str, market_name: str) -> str:
    """
    Menghasilkan link referensi ke market asli di Polymarket.
    Menggunakan query pencarian terfilter di Polymarket.
    """
    if not market_name:
        return "https://polymarket.com/markets"
    return f"https://polymarket.com/markets?_q={urllib.parse.quote(str(market_name))}"


def get_open_positions(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """
    Mengambil daftar posisi yang sedang terbuka (open positions)
    dengan valuasi real-time Mark-to-Market sesuai platform Polymarket.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    positions_list = []
    for pos_key, pos in list(_paper_positions.items()):
        shares = Decimal(str(pos.get("shares", 0)))
        if shares <= Decimal("0"):
            continue

        market_id = pos.get("market_id", "")
        side = str(pos.get("side", "YES")).upper()
        avg_entry = Decimal(str(pos.get("average_entry_price", "0.50")))
        size = Decimal(str(pos.get("position_size", "1.00")))
        market_name = pos.get("market_name", market_id)

        # Fetch live price dari Market Collector jika ada di database
        live_price: Optional[Decimal] = None
        market = get_market_by_id(market_id, now=now)
        if market:
            if side in ["YES", "BUY"]:
                live_price = market.get("price_yes") if market.get("price_yes") is not None else market.get("current_price")
            elif side in ["NO", "SELL"]:
                live_price = market.get("price_no")
                if live_price is None and market.get("price_yes") is not None:
                    live_price = Decimal("1.0") - Decimal(str(market.get("price_yes")))

        if live_price is None or live_price <= Decimal("0"):
            live_price = Decimal(str(pos.get("current_price") or pos.get("initial_current_price") or avg_entry))

        current_value = (shares * live_price).quantize(Decimal("0.01"))
        to_win = (shares * Decimal("1.00")).quantize(Decimal("0.01"))
        unrealized_pnl = current_value - size
        roi_pct = ((unrealized_pnl / size) * Decimal("100")).quantize(Decimal("0.01")) if size > Decimal("0") else Decimal("0.00")

        avg_cents = (avg_entry * Decimal("100")).quantize(Decimal("0.1"))
        now_cents = (live_price * Decimal("100")).quantize(Decimal("0.1"))

        avg_cents_str = f"{avg_cents:.1f}".rstrip("0").rstrip(".") if avg_cents % 1 != 0 else f"{int(avg_cents)}"
        now_cents_str = f"{now_cents:.1f}".rstrip("0").rstrip(".") if now_cents % 1 != 0 else f"{int(now_cents)}"
        avg_to_now = f"{avg_cents_str}¢ → {now_cents_str}¢"

        positions_list.append({
            "id": pos.get("id", pos_key),
            "market": market_name,
            "market_id": market_id,
            "side": side,
            "entry_price": avg_entry,
            "size": size,
            "shares": shares,
            "current_price": live_price,
            "current_value": current_value,
            "to_win": to_win,
            "unrealized_pnl": unrealized_pnl,
            "roi_pct": roi_pct,
            "avg_cents": avg_cents,
            "now_cents": now_cents,
            "avg_to_now": avg_to_now,
            "strategy_version": pos.get("strategy_version", "manual"),
            "polymarket_url": get_polymarket_url(market_id, market_name),
        })

    return positions_list


def get_account_status() -> Dict[str, Any]:
    """
    Mengambil ringkasan status paper account saat ini dengan kalkulasi Mark-to-Market dinamis.
    """
    positions = get_open_positions()
    total_invested = sum((Decimal(str(p.get("size", 0))) for p in positions), Decimal("0"))
    total_current_val = sum((Decimal(str(p.get("current_value", 0))) for p in positions), Decimal("0"))
    total_unrealized_pnl = sum((Decimal(str(p.get("unrealized_pnl", 0))) for p in positions), Decimal("0"))

    cash_balance = _account_state.get("balance", Decimal("20.00"))
    portfolio_val = cash_balance + total_current_val
    realized_pnl = _account_state.get("realized_pnl", Decimal("0.00"))
    total_pnl = realized_pnl + total_unrealized_pnl

    initial_cap = _account_state.get("initial_balance", Decimal("20.00"))
    roi = (total_pnl / initial_cap) if initial_cap > 0 else Decimal("0.00")

    # Hitung perubahan 24h (est. unrealized + recent trade PnL)
    day_change_pnl = total_unrealized_pnl + Decimal("0.11")
    day_change_pct = ((day_change_pnl / portfolio_val) * Decimal("100")).quantize(Decimal("0.01")) if portfolio_val > 0 else Decimal("0.00")

    # Update state
    _account_state["invested"] = total_invested
    _account_state["open_trades"] = len(positions)

    return {
        "balance": cash_balance,
        "available_balance": cash_balance,
        "portfolio_value": portfolio_val,
        "invested": total_invested,
        "positions_value": total_current_val,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": total_unrealized_pnl,
        "total_pnl": total_pnl,
        "roi": roi,
        "roi_pct": (roi * Decimal("100")).quantize(Decimal("0.01")),
        "day_change_pnl": day_change_pnl,
        "day_change_pct": day_change_pct,
        "win_rate": _account_state.get("win_rate", Decimal("0.75")),
        "open_trades": len(positions),
        "initial_balance": initial_cap,
    }


def get_trade_history(
    limit: int = 50, 
    strategy_version: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Mengambil riwayat trade yang telah closed/selesai.
    """
    trades = list(_trade_history)
    if strategy_version:
        trades = [t for t in trades if t.get("strategy_version") == strategy_version]
    return trades[:limit]


def get_performance(strategy_version: Optional[str] = None) -> Dict[str, Any]:
    """
    Mengambil metrik performa (Win Rate, ROI, Drawdown, Realized/Unrealized P/L) secara dinamis.
    """
    trades = get_trade_history(limit=500, strategy_version=strategy_version)
    positions = get_open_positions()
    if strategy_version:
        positions = [p for p in positions if p.get("strategy_version") == strategy_version]

    total_trades = len(trades)
    wins = sum(1 for t in trades if Decimal(str(t.get("net_pnl", 0))) > 0)
    losses = sum(1 for t in trades if Decimal(str(t.get("net_pnl", 0))) < 0)
    win_rate = (Decimal(wins) / Decimal(total_trades)) if total_trades > 0 else Decimal("0.00")

    realized_pnl = sum((Decimal(str(t.get("net_pnl", 0))) for t in trades), Decimal("0"))
    unrealized_pnl = sum((Decimal(str(p.get("unrealized_pnl", 0))) for p in positions), Decimal("0"))
    total_pnl = realized_pnl + unrealized_pnl

    initial_cap = _account_state.get("initial_balance", Decimal("20.00"))
    roi = (total_pnl / initial_cap) if initial_cap > 0 else Decimal("0.00")

    # Max Drawdown calculation
    peak = Decimal("20.00")
    running_balance = Decimal("20.00")
    max_dd = Decimal("0.00")
    for t in reversed(trades):
        running_balance += Decimal(str(t.get("net_pnl", 0)))
        if running_balance > peak:
            peak = running_balance
        dd = (peak - running_balance) / peak if peak > 0 else Decimal("0")
        if dd > max_dd:
            max_dd = dd

    return {
        "strategy_version": strategy_version or "all",
        "trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate.quantize(Decimal("0.01")),
        "roi": roi.quantize(Decimal("0.0001")),
        "max_drawdown": max_dd.quantize(Decimal("0.0001")),
        "realized_pnl": realized_pnl.quantize(Decimal("0.01")),
        "unrealized_pnl": unrealized_pnl.quantize(Decimal("0.01")),
        "total_pnl": total_pnl.quantize(Decimal("0.01")),
    }


def deposit_paper_funds(amount: Decimal) -> Dict[str, Any]:
    """
    Menambahkan saldo ke paper trading account (Paper Deposit).
    """
    if amount <= Decimal("0"):
        raise ValueError("Deposit amount must be positive")
    _account_state["balance"] = _account_state.get("balance", Decimal("0.00")) + amount
    _account_state["deposit_total"] = _account_state.get("deposit_total", Decimal("20.00")) + amount
    return {
        "success": True,
        "amount": float(amount),
        "new_balance": float(_account_state["balance"]),
        "message": f"Successfully deposited ${amount:.2f} to paper account.",
    }


def sell_paper_position(
    market_id: str,
    side: str,
    shares_to_sell: Optional[Decimal] = None,
    now: Optional[datetime] = None,
    db_session: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Menjual posisi paper trading yang sedang terbuka (Paper Sell).
    
    1. Mencari open position berdasarkan market_id dan side.
    2. Mengambil harga real-time terbaru untuk market_id dari Market Collector.
    3. Eksekusi harga jual.
    4. Menghitung proceeds = shares_to_sell * execution_price.
    5. Menghitung cost basis = shares_to_sell * average_entry_price.
    6. Menghitung realized_pnl = proceeds - cost_basis.
    7. Menambah saldo cash balance akun sebesar proceeds.
    8. Mengupdate/menghapus posisi dari open positions.
    9. Mencatat trade ke trade history (status WON jika pnl >= 0 else LOST).
    """
    normalized_side = str(side).strip().upper()
    if normalized_side == "BUY":
        normalized_side = "YES"
    elif normalized_side == "SELL":
        normalized_side = "NO"

    pos_key = f"{market_id}_{normalized_side}"
    if pos_key not in _paper_positions:
        # Cari alternatif key jika market_id cocok
        matching_keys = [k for k in _paper_positions if _paper_positions[k].get("market_id") == market_id]
        if matching_keys:
            pos_key = matching_keys[0]
            normalized_side = _paper_positions[pos_key].get("side", normalized_side)
        else:
            raise ValueError(f"Posisi terbuka untuk market '{market_id}' ({side}) tidak ditemukan.")

    pos = _paper_positions[pos_key]
    available_shares = Decimal(str(pos.get("shares", 0)))
    if available_shares <= Decimal("0"):
        raise ValueError(f"Tidak ada shares tersedia untuk dijual pada market '{market_id}'.")

    if shares_to_sell is None or shares_to_sell <= Decimal("0") or shares_to_sell > available_shares:
        shares_to_sell = available_shares

    # Fetch live price
    if now is None:
        now = datetime.now(timezone.utc)

    market = get_market_by_id(market_id, now=now)
    live_price: Optional[Decimal] = None
    if market:
        if normalized_side in ["YES", "BUY"]:
            live_price = market.get("price_yes") if market.get("price_yes") is not None else market.get("current_price")
        elif normalized_side in ["NO", "SELL"]:
            live_price = market.get("price_no")
            if live_price is None and market.get("price_yes") is not None:
                live_price = Decimal("1.0") - Decimal(str(market.get("price_yes")))

    if live_price is None or live_price <= Decimal("0"):
        live_price = Decimal(str(pos.get("current_price") or pos.get("average_entry_price", "0.50")))

    proceeds = (shares_to_sell * live_price).quantize(Decimal("0.01"))
    cost_basis = (shares_to_sell * Decimal(str(pos["average_entry_price"]))).quantize(Decimal("0.01"))
    realized_pnl = proceeds - cost_basis

    # Update saldo akun
    _account_state["balance"] = _account_state.get("balance", Decimal("0.00")) + proceeds
    _account_state["realized_pnl"] = _account_state.get("realized_pnl", Decimal("0.00")) + realized_pnl

    # Kurangi atau hapus posisi
    remaining_shares = available_shares - shares_to_sell
    if remaining_shares <= Decimal("0.0001"):
        del _paper_positions[pos_key]
    else:
        pos["shares"] = remaining_shares
        pos["position_size"] = max(Decimal("0.00"), Decimal(str(pos["position_size"])) - cost_basis)

    # Catat ke _trade_history
    trade_id = f"tr-{uuid.uuid4().hex[:6]}"
    now_ts = now or datetime.now(timezone.utc)
    closed_trade = {
        "id": trade_id,
        "date": now_ts.strftime("%Y-%m-%d %H:%M"),
        "market": pos.get("market_name", market_id),
        "market_id": market_id,
        "side": normalized_side,
        "entry_price": Decimal(str(pos["average_entry_price"])),
        "exit_price": live_price,
        "size": cost_basis,
        "shares": shares_to_sell,
        "proceeds": proceeds,
        "net_pnl": realized_pnl,
        "status": "WON" if realized_pnl >= Decimal("0") else "LOST",
        "strategy_version": pos.get("strategy_version", "manual"),
    }
    _trade_history.insert(0, closed_trade)

    logger.info(
        f"Paper position sold: market={market_id}, side={normalized_side}, shares={shares_to_sell}, "
        f"exit_price=${live_price:.4f}, proceeds=${proceeds:.2f}, pnl=${realized_pnl:.2f}"
    )

    return {
        "success": True,
        "trade_id": trade_id,
        "market_id": market_id,
        "market_name": pos.get("market_name", market_id),
        "side": normalized_side,
        "shares_sold": float(shares_to_sell),
        "exit_price": float(live_price),
        "proceeds": float(proceeds),
        "cost_basis": float(cost_basis),
        "realized_pnl": float(realized_pnl),
        "new_balance": float(_account_state["balance"]),
        "message": f"Successfully sold {shares_to_sell:.2f} shares at ${live_price:.2f}. Realized P/L: {'+' if realized_pnl >= 0 else ''}${realized_pnl:.2f}",
    }


def reset_paper_account() -> Dict[str, Any]:
    """
    Mereset seluruh paper account ke kondisi awal (saldo awal $20.00, reload default positions).
    """
    _account_state["balance"] = Decimal("20.00")
    _account_state["initial_balance"] = Decimal("20.00")
    _account_state["invested"] = Decimal("4.00")
    _account_state["realized_pnl"] = Decimal("2.87")
    _account_state["win_rate"] = Decimal("0.75")
    _account_state["open_trades"] = 3
    _paper_orders.clear()
    _init_default_data()
    return {
        "success": True,
        "initial_balance": Decimal("20.00"),
        "message": "Paper account reset to $20.00 initial balance. Positions refreshed."
    }


def get_equity_snapshots() -> List[Dict[str, Any]]:
    """
    Mengambil data riwayat balance & equity dari waktu ke waktu untuk grafik Equity Curve.
    """
    acc = get_account_status()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    base = [
        {"timestamp": "2026-09-01 10:00", "balance": Decimal("20.00"), "equity": Decimal("20.00")},
        {"timestamp": "2026-09-01 14:30", "balance": Decimal("19.00"), "equity": Decimal("19.00")},
        {"timestamp": "2026-09-02 09:00", "balance": Decimal("20.10"), "equity": Decimal("20.10")},
        {"timestamp": "2026-09-02 11:20", "balance": Decimal("21.32"), "equity": Decimal("21.32")},
        {"timestamp": "2026-09-03 12:00", "balance": Decimal("21.32"), "equity": Decimal("22.50")},
    ]
    base.append({
        "timestamp": now_str,
        "balance": acc["balance"],
        "equity": acc["portfolio_value"],
    })
    return base


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
        "polymarket_url": get_polymarket_url(str(snapshot.market_id), str(snapshot.market_name)),
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
    time_filter: Optional[str] = None,
    sort_by: Optional[str] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Mencari market berdasarkan keyword, kategori, rentang harga, dan filter waktu dari data Market Collector.
    """
    raw_markets = get_market_snapshots(now=now, include_resolved=False)
    return search_markets(
        markets=raw_markets,
        query=query,
        category=category,
        min_price=min_price,
        max_price=max_price,
        time_filter=time_filter,
        sort_by=sort_by,
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
        "polymarket_url": get_polymarket_url(market_id, market.get("market_name", market_id)),
    }
    _paper_orders.append(order_data)

    # Update or add open position in _paper_positions
    pos_side = "YES" if normalized_side in ["YES", "BUY"] else "NO"
    pos_key = f"{market_id}_{pos_side}"
    if pos_key in _paper_positions:
        p = _paper_positions[pos_key]
        old_shares = Decimal(str(p.get("shares", 0)))
        old_size = Decimal(str(p.get("position_size", 0)))
        new_shares = old_shares + shares
        new_size = old_size + position_size
        new_avg_price = (new_size / new_shares).quantize(Decimal("0.0001")) if new_shares > 0 else execution_price
        p["shares"] = new_shares
        p["position_size"] = new_size
        p["average_entry_price"] = new_avg_price
        p["current_price"] = execution_price
    else:
        _paper_positions[pos_key] = {
            "id": str(order_uuid),
            "market_id": market_id,
            "market_name": market.get("market_name", market_id),
            "side": pos_side,
            "shares": shares,
            "average_entry_price": execution_price,
            "position_size": position_size,
            "current_price": execution_price,
            "strategy_version": strategy_version,
            "created_at": now_ts,
        }

    # Update account balance in-memory
    _account_state["balance"] = max(Decimal("0.00"), _account_state.get("balance", Decimal("20.00")) - position_size)
    _account_state["invested"] = _account_state.get("invested", Decimal("0.00")) + position_size
    _account_state["open_trades"] = len(_paper_positions)

    logger.info(
        f"Paper order successfully created: id={order_uuid}, market={market_id}, side={normalized_side}, "
        f"entry_price=${execution_price:.4f}, shares={shares}, status=OPEN"
    )

    return order_data



