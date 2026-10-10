"""
Paper Trading Service Layer.

Seluruh state akun (saldo, posisi, order, trade, histori equity) disimpan di database
(tabel paper_*), sehingga dashboard, CLI, dan bot Telegram membaca sumber data yang sama
dan data tetap ada setelah restart.
"""
import threading
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Iterable, Iterator, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.metrics import calculate_performance_metrics
from app.paper_trading.models import (
    MarketLatest,
    MarketResolution,
    MarketSnapshot,
    PaperAccount,
    PaperBalanceSnapshot,
    PaperCashMovement,
    PaperOrder,
    PaperOrderStatus,
    PaperPosition,
    PaperTrade,
    PaperTradeStatus,
    TradeSide,
)
from app.paper_trading.settlement_engine import (
    apply_slippage_and_spread,
    calculate_settlement,
    calculate_shares,
    evaluate_risk_and_rules,
)
from app.paper_trading.suggestions import search_markets
from app.paper_trading.weather_peaks import (
    city_volume_summary,
    filter_peak_time_suggestions,
    top_cities_by_volume,
    upcoming_recommendation_windows,
)

logger = get_logger("paper_service")

DEFAULT_ACCOUNT_NAME = "default"
QUANT = Decimal("0.0001")
DUST_SHARES = Decimal("0.0001")

# Serialisasi operasi tulis dalam satu proses. Antar proses dilindungi oleh
# row lock `SELECT ... FOR UPDATE` pada baris akun (PostgreSQL).
_write_lock = threading.RLock()


def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(QUANT, rounding=ROUND_HALF_UP)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _normalize_side(side: str) -> str:
    """Normalisasi sisi outcome menjadi 'YES' / 'NO'. Raise ValueError jika tidak valid."""
    normalized = str(side).strip().upper()
    if normalized in ("YES", "BUY"):
        return "YES"
    if normalized in ("NO", "SELL"):
        return "NO"
    raise ValueError(f"Side '{side}' tidak valid. Gunakan 'YES' atau 'NO'.")


def _side_value(side: Any) -> str:
    return side.value if hasattr(side, "value") else str(side)


def _outcome_price(market: Optional[Dict[str, Any]], side: str) -> Optional[Decimal]:
    """Harga outcome YES/NO dari snapshot market (NO diturunkan dari 1 - YES jika perlu)."""
    if not market:
        return None
    if side == "YES":
        raw = market.get("price_yes") if market.get("price_yes") is not None else market.get("current_price")
    else:
        raw = market.get("price_no")
        if raw is None and market.get("price_yes") is not None:
            raw = Decimal("1") - Decimal(str(market.get("price_yes")))
    return Decimal(str(raw)) if raw is not None else None


@contextmanager
def _session_scope(db: Optional[Session] = None) -> Iterator[Session]:
    """Memakai session yang diberikan, atau membuka session baru yang ditutup setelah selesai."""
    if db is not None:
        yield db
        return
    session = get_db_session()
    try:
        yield session
    finally:
        session.close()


def _get_account(db: Session, for_update: bool = False) -> PaperAccount:
    """Mengambil akun paper default; membuatnya dengan INITIAL_BALANCE jika belum ada."""
    query = db.query(PaperAccount).order_by(PaperAccount.created_at, PaperAccount.id)
    if for_update:
        query = query.with_for_update()
    account = query.first()
    if account is None:
        initial = _q(Decimal(str(settings.INITIAL_BALANCE)))
        account = PaperAccount(
            name=DEFAULT_ACCOUNT_NAME,
            initial_balance=initial,
            current_balance=initial,
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            total_fees=Decimal("0"),
        )
        db.add(account)
        db.flush()
        db.add(PaperBalanceSnapshot(account_id=account.id, balance=initial, equity=initial, timestamp=_utcnow()))
        db.commit()
        if for_update:
            account = db.query(PaperAccount).filter(PaperAccount.id == account.id).with_for_update().one()
    return account


def ensure_default_account() -> None:
    """Memastikan akun paper default ada (dipanggil saat startup)."""
    with _write_lock, _session_scope() as db:
        _get_account(db)


def _total_capital(db: Session, account: PaperAccount) -> Decimal:
    """Modal awal + seluruh deposit. Dipakai sebagai basis ROI."""
    deposits = (
        db.query(func.coalesce(func.sum(PaperCashMovement.amount), 0))
        .filter(PaperCashMovement.account_id == account.id, PaperCashMovement.kind == "DEPOSIT")
        .scalar()
    )
    return Decimal(str(account.initial_balance)) + Decimal(str(deposits or 0))


def _open_exposure(db: Session, account: PaperAccount, market_id: Optional[str] = None) -> Decimal:
    query = db.query(func.coalesce(func.sum(PaperPosition.position_size), 0)).filter(
        PaperPosition.account_id == account.id, PaperPosition.shares > 0
    )
    if market_id is not None:
        query = query.filter(PaperPosition.market_id == market_id)
    return Decimal(str(query.scalar() or 0))


def _record_balance_snapshot(db: Session, account: PaperAccount, trade_id: Optional[uuid.UUID] = None,
                             now: Optional[datetime] = None) -> None:
    """Mencatat titik equity curve (cash + nilai posisi terbuka pada harga pasar terkini)."""
    positions_value = sum(
        (Decimal(str(p["current_value"])) for p in _build_positions(db, account, now=now)), Decimal("0")
    )
    balance = Decimal(str(account.current_balance))
    db.add(PaperBalanceSnapshot(
        account_id=account.id,
        trade_id=trade_id,
        balance=balance,
        equity=_q(balance + positions_value),
        timestamp=now or _utcnow(),
    ))


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


def _format_cents(value: Decimal) -> str:
    cents = (value * Decimal("100")).quantize(Decimal("0.1"))
    return f"{int(cents)}" if cents % 1 == 0 else f"{cents:.1f}"


def _build_positions(db: Session, account: PaperAccount, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Valuasi Mark-to-Market seluruh posisi terbuka memakai satu query harga terbaru."""
    now = now or _utcnow()
    rows = (
        db.query(PaperPosition)
        .filter(PaperPosition.account_id == account.id, PaperPosition.shares > 0)
        .order_by(PaperPosition.created_at)
        .all()
    )
    if not rows:
        return []

    market_ids = {r.market_id for r in rows}
    latest = get_latest_markets(market_ids, now=now, db=db)
    resolutions = {
        r.market_id: r.winning_outcome
        for r in db.query(MarketResolution).filter(MarketResolution.market_id.in_(market_ids)).all()
    }

    positions_list = []
    for pos in rows:
        side = _side_value(pos.side)
        shares = Decimal(str(pos.shares))
        avg_entry = Decimal(str(pos.average_entry_price))
        size = Decimal(str(pos.position_size))
        market = latest.get(pos.market_id)
        market_name = pos.market_name or (market or {}).get("market_name") or pos.market_id

        winner = resolutions.get(pos.market_id)
        if winner in ("YES", "NO"):
            # Market sudah resolve, menunggu settlement: nilai = payout final
            live_price = Decimal("1") if winner == side else Decimal("0")
            price_source = "resolved"
        else:
            live_price = _outcome_price(market, side)
            price_source = "market"
            if live_price is None or live_price <= 0:
                # Tanpa data harga: tandai posisi pada harga pokok (tidak mengarang untung/rugi)
                live_price = avg_entry
                price_source = "cost"

        current_value = _q(shares * live_price)
        unrealized_pnl = _q(current_value - size)
        roi_pct = ((unrealized_pnl / size) * Decimal("100")).quantize(Decimal("0.01")) if size > 0 else Decimal("0.00")

        positions_list.append({
            "id": str(pos.id),
            "market": market_name,
            "market_id": pos.market_id,
            "side": side,
            "entry_price": avg_entry,
            "size": _q(size),
            "shares": _q(shares),
            "current_price": live_price,
            "current_value": current_value,
            "to_win": _q(shares),
            "unrealized_pnl": unrealized_pnl,
            "roi_pct": roi_pct,
            "avg_cents": (avg_entry * Decimal("100")).quantize(Decimal("0.1")),
            "now_cents": (live_price * Decimal("100")).quantize(Decimal("0.1")),
            "avg_to_now": f"{_format_cents(avg_entry)}¢ → {_format_cents(live_price)}¢",
            "price_source": price_source,
            "is_stale": bool((market or {}).get("is_stale", False)),
            "strategy_version": pos.strategy_version or "manual",
            "polymarket_url": get_polymarket_url(pos.market_id, market_name),
        })
    return positions_list


def get_open_positions(now: Optional[datetime] = None, db: Optional[Session] = None) -> List[Dict[str, Any]]:
    """
    Mengambil daftar posisi yang sedang terbuka (open positions)
    dengan valuasi real-time Mark-to-Market sesuai platform Polymarket.
    """
    with _session_scope(db) as session:
        return _build_positions(session, _get_account(session), now=now)


def _closed_trades(db: Session, account: PaperAccount, strategy_version: Optional[str] = None,
                   limit: Optional[int] = None) -> List[PaperTrade]:
    query = db.query(PaperTrade).filter(
        PaperTrade.account_id == account.id, PaperTrade.status != PaperTradeStatus.OPEN
    )
    if strategy_version:
        query = query.filter(PaperTrade.strategy_version == strategy_version)
    query = query.order_by(PaperTrade.closed_at.desc(), PaperTrade.opened_at.desc())
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def _trade_to_dict(t: PaperTrade) -> Dict[str, Any]:
    closed = _aware(t.closed_at) or _aware(t.opened_at)
    market_name = t.market_name or t.market_id
    return {
        "id": str(t.id),
        "date": closed.strftime("%Y-%m-%d %H:%M") if closed else "-",
        "market": market_name,
        "market_id": t.market_id,
        "side": _side_value(t.side),
        "entry_price": Decimal(str(t.entry_price)),
        "exit_price": Decimal(str(t.exit_price)) if t.exit_price is not None else None,
        "size": _q(Decimal(str(t.position_size))),
        "shares": _q(Decimal(str(t.shares))),
        "fees": _q(Decimal(str(t.fees or 0))),
        "net_pnl": _q(Decimal(str(t.net_pnl or 0))),
        "status": _side_value(t.status),
        "strategy_version": t.strategy_version,
        "polymarket_url": get_polymarket_url(t.market_id, market_name),
    }


def _stats(db: Session, account: PaperAccount, positions: List[Dict[str, Any]],
           strategy_version: Optional[str] = None) -> Dict[str, Any]:
    """Satu sumber perhitungan metrik untuk status akun dan performa."""
    trades = [_trade_to_dict(t) for t in _closed_trades(db, account, strategy_version)]
    if strategy_version:
        positions = [p for p in positions if p.get("strategy_version") == strategy_version]
    equity_curve = [
        Decimal(str(s.equity))
        for s in db.query(PaperBalanceSnapshot)
        .filter(PaperBalanceSnapshot.account_id == account.id)
        .order_by(PaperBalanceSnapshot.timestamp)
        .all()
    ]
    capital = _total_capital(db, account)
    metrics = calculate_performance_metrics(trades=trades, equity_curve=equity_curve, initial_balance=capital)
    unrealized = sum((Decimal(str(p["unrealized_pnl"])) for p in positions), Decimal("0"))
    return {
        "trades": trades,
        "metrics": metrics,
        "unrealized_pnl": _q(unrealized),
        "capital": capital,
    }


def get_account_status(now: Optional[datetime] = None, db: Optional[Session] = None) -> Dict[str, Any]:
    """
    Mengambil ringkasan status paper account saat ini dengan kalkulasi Mark-to-Market dinamis.
    """
    now = now or _utcnow()
    with _session_scope(db) as session:
        account = _get_account(session)
        positions = _build_positions(session, account, now=now)
        stats = _stats(session, account, positions)

        total_invested = sum((Decimal(str(p["size"])) for p in positions), Decimal("0"))
        positions_value = sum((Decimal(str(p["current_value"])) for p in positions), Decimal("0"))
        cash_balance = _q(Decimal(str(account.current_balance)))
        portfolio_val = _q(cash_balance + positions_value)
        realized_pnl = _q(Decimal(str(account.realized_pnl)))
        total_pnl = _q(realized_pnl + stats["unrealized_pnl"])
        capital = stats["capital"]
        roi = (total_pnl / capital) if capital > 0 else Decimal("0")

        # Perubahan 24 jam: equity sekarang vs titik equity terakhir sebelum 24 jam lalu
        baseline = (
            session.query(PaperBalanceSnapshot)
            .filter(PaperBalanceSnapshot.account_id == account.id,
                    PaperBalanceSnapshot.timestamp <= now - timedelta(hours=24))
            .order_by(PaperBalanceSnapshot.timestamp.desc())
            .first()
        ) or (
            session.query(PaperBalanceSnapshot)
            .filter(PaperBalanceSnapshot.account_id == account.id)
            .order_by(PaperBalanceSnapshot.timestamp)
            .first()
        )
        baseline_equity = Decimal(str(baseline.equity)) if baseline else capital
        day_change_pnl = _q(portfolio_val - baseline_equity)
        day_change_pct = (
            ((day_change_pnl / baseline_equity) * Decimal("100")).quantize(Decimal("0.01"))
            if baseline_equity > 0 else Decimal("0.00")
        )

        return {
            "balance": cash_balance,
            "available_balance": cash_balance,
            "portfolio_value": portfolio_val,
            "invested": _q(total_invested),
            "positions_value": _q(positions_value),
            "realized_pnl": realized_pnl,
            "unrealized_pnl": stats["unrealized_pnl"],
            "total_pnl": total_pnl,
            "total_fees": _q(Decimal(str(account.total_fees))),
            "roi": roi.quantize(QUANT),
            "roi_pct": (roi * Decimal("100")).quantize(Decimal("0.01")),
            "day_change_pnl": day_change_pnl,
            "day_change_pct": day_change_pct,
            "win_rate": stats["metrics"]["win_rate"],
            "open_trades": len(positions),
            "initial_balance": _q(Decimal(str(account.initial_balance))),
            "total_capital": _q(capital),
        }


def get_trade_history(
    limit: int = 50,
    strategy_version: Optional[str] = None,
    db: Optional[Session] = None,
) -> List[Dict[str, Any]]:
    """
    Mengambil riwayat trade yang telah closed/selesai (terbaru lebih dulu).
    """
    with _session_scope(db) as session:
        account = _get_account(session)
        return [_trade_to_dict(t) for t in _closed_trades(session, account, strategy_version, limit)]


def get_performance(strategy_version: Optional[str] = None, db: Optional[Session] = None) -> Dict[str, Any]:
    """
    Mengambil metrik performa (Win Rate, ROI, Drawdown, Realized/Unrealized P/L) secara dinamis.
    """
    with _session_scope(db) as session:
        account = _get_account(session)
        positions = _build_positions(session, account)
        stats = _stats(session, account, positions, strategy_version)
        m = stats["metrics"]
        realized = m["realized_pnl"]
        total_pnl = realized + stats["unrealized_pnl"]
        capital = stats["capital"]
        roi = (total_pnl / capital) if capital > 0 else Decimal("0")
        return {
            "strategy_version": strategy_version or "all",
            "trades": m["total_closed_trades"],
            "wins": m["wins"],
            "losses": m["losses"],
            "win_rate": m["win_rate"].quantize(Decimal("0.01")),
            "roi": roi.quantize(QUANT),
            "max_drawdown": m["max_drawdown_percentage"],
            "realized_pnl": realized.quantize(Decimal("0.01")),
            "unrealized_pnl": stats["unrealized_pnl"].quantize(Decimal("0.01")),
            "total_pnl": total_pnl.quantize(Decimal("0.01")),
        }


def deposit_paper_funds(amount: Decimal) -> Dict[str, Any]:
    """
    Menambahkan saldo ke paper trading account (Paper Deposit).
    Deposit dicatat di ledger dan menambah basis modal untuk perhitungan ROI.
    """
    amount = _q(Decimal(str(amount)))
    if amount <= Decimal("0"):
        raise ValueError("Deposit amount must be positive")
    with _write_lock, _session_scope() as db:
        try:
            account = _get_account(db, for_update=True)
            account.current_balance = Decimal(str(account.current_balance)) + amount
            db.add(PaperCashMovement(account_id=account.id, kind="DEPOSIT", amount=amount, timestamp=_utcnow()))
            db.flush()
            _record_balance_snapshot(db, account)
            db.commit()
            new_balance = Decimal(str(account.current_balance))
        except Exception:
            db.rollback()
            raise
    return {
        "success": True,
        "amount": float(amount),
        "new_balance": float(new_balance),
        "message": f"Successfully deposited ${amount:.2f} to paper account.",
    }


def reset_paper_account() -> Dict[str, Any]:
    """
    Mereset paper account ke kondisi awal: saldo = INITIAL_BALANCE, tanpa posisi,
    order, trade, deposit, maupun histori equity.
    """
    initial = _q(Decimal(str(settings.INITIAL_BALANCE)))
    with _write_lock, _session_scope() as db:
        try:
            account = _get_account(db, for_update=True)
            for model in (PaperBalanceSnapshot, PaperTrade, PaperPosition, PaperOrder, PaperCashMovement):
                db.query(model).filter(model.account_id == account.id).delete(synchronize_session=False)
            account.initial_balance = initial
            account.current_balance = initial
            account.realized_pnl = Decimal("0")
            account.unrealized_pnl = Decimal("0")
            account.total_fees = Decimal("0")
            db.add(PaperBalanceSnapshot(account_id=account.id, balance=initial, equity=initial, timestamp=_utcnow()))
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {
        "success": True,
        "initial_balance": initial,
        "message": f"Paper account reset to ${initial:.2f} initial balance. All positions and trades cleared."
    }


def get_equity_snapshots(limit: int = 500) -> List[Dict[str, Any]]:
    """
    Mengambil data riwayat balance & equity dari waktu ke waktu untuk grafik Equity Curve,
    ditambah satu titik "sekarang" berdasarkan valuasi pasar terkini.
    """
    with _session_scope() as db:
        account = _get_account(db)
        rows = (
            db.query(PaperBalanceSnapshot)
            .filter(PaperBalanceSnapshot.account_id == account.id)
            .order_by(PaperBalanceSnapshot.timestamp.desc())
            .limit(limit)
            .all()
        )
        points = [
            {
                "timestamp": _aware(s.timestamp).strftime("%Y-%m-%d %H:%M"),
                "balance": _q(Decimal(str(s.balance))),
                "equity": _q(Decimal(str(s.equity))),
            }
            for s in reversed(rows)
        ]
        acc = get_account_status(db=db)
        points.append({
            "timestamp": _utcnow().strftime("%Y-%m-%d %H:%M"),
            "balance": acc["balance"],
            "equity": acc["portfolio_value"],
        })
        return points


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
        "outcome_yes_label": snapshot.outcome_yes_label or "Yes",
        "outcome_no_label": snapshot.outcome_no_label or "No",
        "volume": float(snapshot.volume) if getattr(snapshot, "volume", None) is not None else None,
        "yes_token_id": getattr(snapshot, "yes_token_id", None),
        "resolution_station": getattr(snapshot, "resolution_station", None),
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
    Mengambil observasi pasar terbaru per market_id dari tabel market_latest
    (satu baris per market, tanpa memindai histori market_snapshots).
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
        query = db.query(MarketLatest)
        if not include_resolved:
            query = query.filter(
                MarketLatest.is_resolved.is_(False),
                func.lower(MarketLatest.status) != "resolved",
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
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    now: Optional[datetime] = None,
    phase: str = "pre",
) -> List[Dict[str, Any]]:
    """
    Rekomendasi per event suhu (kota + highest/lowest + tanggal) yang sedang berada di jendela
    menjelang jam puncak suhu lokal kotanya. Tanpa filter harga kecuali min/max diberikan.
    """
    raw_markets = get_market_snapshots(now=now, include_resolved=False)
    events = filter_peak_time_suggestions(markets=raw_markets, min_price=min_price, max_price=max_price, now=now,
                                          phase=phase)
    from app.paper_trading.live_market_data import enrich_suggestions
    return enrich_suggestions(_with_city_volume_rank(events, raw_markets, now))


def _with_city_volume_rank(items: List[Dict[str, Any]], raw_markets, now: Optional[datetime]) -> List[Dict[str, Any]]:
    """city_volume_rank = peringkat kota menurut total volume market suhu (1 = terbesar; None tanpa data)."""
    ranking = top_cities_by_volume(raw_markets, limit=0, now=now) or []
    rank = {city: i for i, city in enumerate(ranking, start=1)}
    for item in items:
        item["city_volume_rank"] = rank.get(item["city"])
    return items


def get_top_volume_cities(limit: int, now: Optional[datetime] = None) -> Optional[List[str]]:
    """Kota dengan total volume market suhu open terbesar; None jika data volume belum ada."""
    return top_cities_by_volume(get_market_snapshots(now=now, include_resolved=False), limit=limit, now=now)


def get_city_volume_summary(limit: int = 7, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Kota dengan total volume market suhu open terbesar (dengan rincian tertinggi/terendah)."""
    summary = city_volume_summary(get_market_snapshots(now=now, include_resolved=False), now=now)
    return summary[:limit] if limit > 0 else summary


def get_city_stations(now: Optional[datetime] = None) -> Dict[str, Dict[str, str]]:
    """{kota: {station, unit}} dari market suhu open (stasiun resolusi & satuan bracket-nya)."""
    from collections import Counter

    from app.paper_trading.cities import resolve_city
    from app.paper_trading.weather_peaks import _open_temperature_markets, bracket_label

    now = now or datetime.now(timezone.utc)
    stations: Dict[str, Counter] = {}
    units: Dict[str, Counter] = {}
    for m, name, parsed in _open_temperature_markets(get_market_snapshots(now=now, include_resolved=False), now):
        city = resolve_city(parsed.city)
        if m.get("resolution_station"):
            stations.setdefault(city, Counter())[m["resolution_station"]] += 1
        units.setdefault(city, Counter())["F" if "°F" in bracket_label(name) else "C"] += 1
    return {
        city: {"station": counter.most_common(1)[0][0], "unit": units[city].most_common(1)[0][0]}
        for city, counter in stations.items()
    }


def match_city(query: str, cities) -> Optional[str]:
    """Cocokkan input bebas ('nyc', 'hong kong', 'york') ke nama kota market suhu."""
    from app.paper_trading.cities import CITY_ALIASES

    q = " ".join(str(query or "").split()).lower()
    if not q:
        return None
    canonical = next((v for k, v in CITY_ALIASES.items() if k.lower() == q), None)
    for city in cities:
        if city.lower() == q or city == canonical:
            return city
    matches = [c for c in cities if q in c.lower()]
    return sorted(matches, key=len)[0] if matches else None


def search_cities(query: str, cities, rank: Optional[Dict[str, int]] = None) -> List[str]:
    """
    Pencarian kota untuk dashboard: nama persis / alias (nyc, hk) → satu kota; selain itu semua kota yang
    namanya memuat teks pencarian, urut volume market (terbesar dulu) lalu abjad.
    """
    exact = match_city(query, cities)
    q = " ".join(str(query or "").split()).lower()
    if not q:
        return []
    if exact is not None and (exact.lower() == q or q not in exact.lower()):
        return [exact]  # persis atau alias
    rank = rank or {}
    matches = [c for c in cities if q in c.lower()]
    return sorted(matches, key=lambda c: (rank.get(c, 10_000), c))


def get_current_weather(limit: int = 7, city: Optional[str] = None,
                        now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Cuaca terkini di stasiun resolusi market suhu (NOAA METAR / HKO) untuk kota top volume, atau satu
    beberapa kota hasil pencarian `city` (nama/alias/sebagian nama, maks `limit`): suhu sekarang, max/min sejak 00:00 lokal, kondisi, tren °/jam, perkiraan
    & kesimpulan. {"cities": [semua kota berstasiun], "items": [...], "not_found": bool}
    """
    from datetime import timedelta

    from app.paper_trading.live_market_data import fetch_metar_observations, station_report
    from app.paper_trading.weather_peaks import city_timezone

    now = now or datetime.now(timezone.utc)
    stations = get_city_stations(now=now)
    result: Dict[str, Any] = {"cities": sorted(stations), "items": [], "not_found": False}
    ranking = [r["city"] for r in get_city_volume_summary(limit=0, now=now)]
    rank = {c: i for i, c in enumerate(ranking, start=1)}
    if city:
        selected = search_cities(city, stations, rank)[:max(limit, 1)]
        result["query"] = city
        if not selected:
            result["not_found"] = True
            return result
    else:
        selected = [c for c in ranking if c in stations][:limit] if ranking else sorted(stations)[:limit]
    fetch_metar_observations(stations[c]["station"] for c in selected)  # satu request untuk semua stasiun
    for c in selected:
        tz = city_timezone(c)
        info = stations[c]
        report = station_report(info["station"], tz, info["unit"], now=now, city=c) if tz else None
        item = {"city": c, "station": info["station"], "unit": info["unit"], "volume_rank": rank.get(c),
                "local_time": now.astimezone(tz).strftime("%H:%M") if tz else None, "report": report}
        if report and report.get("current_at") is not None:
            item["stale"] = now - report["current_at"] > timedelta(minutes=90)
        result["items"].append(item)
    return result


def get_recommendation_schedule(now: Optional[datetime] = None, limit: int = 10) -> List[Dict[str, Any]]:
    """Jadwal jendela rekomendasi berikutnya per kota (untuk ditampilkan saat belum ada yang aktif)."""
    raw_markets = get_market_snapshots(now=now, include_resolved=False)
    return _with_city_volume_rank(upcoming_recommendation_windows(raw_markets, now=now, limit=limit), raw_markets, now)


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
    Mengambil observasi pasar TERBARU untuk market_id dari tabel market_latest.
    Jika market belum pernah diamati, mengembalikan None.
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
        snapshot = db.get(MarketLatest, str(market_id))
        if snapshot is None:
            return None

        return _format_market_snapshot(snapshot, now=now)
    except Exception as e:
        logger.error(f"Error query snapshot market_id='{market_id}': {e}", exc_info=True)
        return None
    finally:
        if close_session and db is not None:
            db.close()




def get_latest_markets(
    market_ids: Iterable[str],
    now: Optional[datetime] = None,
    db: Optional[Session] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Observasi terbaru (market_latest) untuk sekumpulan market_id dalam SATU query (menghindari N+1).
    """
    ids = {str(m) for m in market_ids if m}
    if not ids:
        return {}
    with _session_scope(db) as session:
        rows = session.query(MarketLatest).filter(MarketLatest.market_id.in_(ids)).all()
        return {r.market_id: _format_market_snapshot(r, now=now) for r in rows}


def get_paper_orders(limit: int = 50, db: Optional[Session] = None) -> List[Dict[str, Any]]:
    """
    Mengambil daftar order paper trading yang telah tersimpan (terbaru lebih dulu).
    """
    with _session_scope(db) as session:
        account = _get_account(session)
        rows = (
            session.query(PaperOrder)
            .filter(PaperOrder.account_id == account.id)
            .order_by(PaperOrder.timestamp.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "order_id": str(o.paper_order_id),
                "market_id": o.market_id,
                "side": _side_value(o.side),
                "status": _side_value(o.status),
                "entry_price": Decimal(str(o.entry_price)),
                "position_size": Decimal(str(o.position_size)),
                "shares": Decimal(str(o.shares)),
                "strategy_version": o.strategy_version,
                "timestamp": _aware(o.timestamp).isoformat(),
            }
            for o in rows
        ]


def _refresh_market_from_source(market_id: str) -> Optional[Dict[str, Any]]:
    """
    Fallback on-demand: jika market (condition id Polymarket, prefix 0x) belum ada di
    market_snapshots, ambil langsung dari Gamma API lalu simpan snapshot-nya.
    """
    if not str(market_id).startswith("0x"):
        return None
    try:
        from app.market_collector.collector import sync_markets_by_condition_ids
        sync_markets_by_condition_ids([market_id])
    except Exception as err:
        logger.warning("Gagal mengambil market %s on-demand dari Gamma API: %s", market_id, err)
        return None
    return get_market_by_id(market_id)


def _notify_async(fn_name: str, payload: Dict[str, Any]) -> None:
    """Kirim notifikasi Telegram di background agar tidak memblokir request/settlement."""
    def _run():
        try:
            from app.paper_trading import telegram
            getattr(telegram, fn_name)(payload)
        except Exception as err:
            logger.warning("Gagal mengirim notifikasi Telegram (%s): %s", fn_name, err)

    threading.Thread(target=_run, daemon=True).start()


def create_paper_order(
    market_id: str,
    side: str,
    position_size: Decimal,
    user_viewed_price: Optional[Decimal] = None,
    strategy_version: str = "manual",
    db_session: Optional[Session] = None,
    now: Optional[datetime] = None,
    execution_price: Optional[Decimal] = None,
    risk_limits: Optional[Dict[str, Decimal]] = None,
    notify: bool = True,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Membuat paper order dengan proteksi Anti Stale Price.

    Auto trader memakai `execution_price` = harga dari order book live (VWAP level ask sesuai ukuran
    order + biaya taker) sebagai harga eksekusi, dan `risk_limits` (max_position_size,
    max_market_exposure, max_total_exposure) sebagai pengganti batas order manual.

    Urutan proses WAJIB:
    1. Fetch harga real-time terbaru dari Market Collector untuk market_id ini.
       (Reject jika market tidak ditemukan, sudah resolved, sudah lewat waktu resolusi,
       atau harga snapshot tidak tersedia / stale).
    2. Hitung selisih harga dengan harga yang dilihat user (HANYA untuk generate warning jika > threshold).
       JANGAN gunakan user_viewed_price untuk kalkulasi settlement/shares/slippage apapun!
    3. apply_slippage_and_spread(historical_mid_price=real_time_price, ...) -> execution_price.
    4. evaluate_risk_and_rules(...) termasuk limit eksposur per market & total
       -> JIKA REJECTED: langsung hentikan proses (raise ValueError), TIDAK ADA partial write.
    5. calculate_shares(position_size, execution_price) -> shares.
    6. Dalam SATU transaksi: simpan paper_orders (FILLED), upsert paper_positions,
       kurangi saldo akun, catat balance snapshot.
    """
    position_size = Decimal(str(position_size))
    if position_size <= Decimal("0"):
        raise ValueError("Position size must be strictly positive")

    now = _aware(now) or _utcnow()

    # 1. Fetch harga real-time terbaru langsung dari Market Collector
    market = get_market_by_id(market_id, now=now)
    if not market:
        market = _refresh_market_from_source(market_id)
    if not market:
        raise ValueError(f"Market '{market_id}' tidak ditemukan di data Market Collector.")

    if market.get("is_resolved") or str(market.get("status", "")).lower() == "resolved":
        raise ValueError(f"Market '{market_id}' sudah resolved dan tidak dapat menerima order.")

    # Catatan: endDate Polymarket bukan batas akhir trading (market cuaca tetap menerima order
    # berjam-jam setelahnya), jadi yang menentukan adalah status dari collector (acceptingOrders).
    if str(market.get("status", "")).lower() != "open":
        raise ValueError(f"Market '{market_id}' sedang tidak menerima order (status: {market.get('status')}).")

    normalized_side = _normalize_side(side)
    raw_price = _outcome_price(market, normalized_side)
    if raw_price is None or raw_price <= Decimal("0") or raw_price >= Decimal("1"):
        raise ValueError(f"Data harga real-time tidak tersedia atau tidak valid untuk market '{market_id}'.")

    # HARGA REAL-TIME TERBARU
    real_time_price = raw_price

    # 2. Audit Trail & Anti Stale Price Warning
    # CATATAN KRUSIAL: user_viewed_price HANYA digunakan untuk logging/audit trail dan memicu warning.
    warning_message: Optional[str] = None
    if user_viewed_price is not None:
        user_p = Decimal(str(user_viewed_price))
        if user_p > Decimal("0"):
            price_divergence = abs(real_time_price - user_p) / user_p
            threshold = Decimal(str(settings.PRICE_DIVERGENCE_WARNING_THRESHOLD))
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
        stale_minutes = int(settings.COLLECTOR_INTERVAL_SECONDS * 3 / 60)
        stale_msg = (
            f"Data harga pasar ini terakhir diperbarui pada {ts_str} "
            f"(berstatus stale > {stale_minutes} menit)."
        )
        if settings.REJECT_STALE_ORDERS:
            logger.warning(f"Order ditolak: market '{market_id}' memakai snapshot stale (ts: {ts_str})")
            raise ValueError(f"REJECTED: {stale_msg} Tunggu collector memperbarui harga.")
        stale_msg = f"Perhatian: {stale_msg}"
        warning_message = f"{warning_message} | {stale_msg}" if warning_message else stale_msg
        logger.warning(f"Anti-Stale Notice: Market '{market_id}' menggunakan data snapshot stale (ts: {ts_str})")

    # 3. apply_slippage_and_spread menggunakan harga real-time (kecuali harga order book diberikan)
    if execution_price is not None:
        execution_price = Decimal(str(execution_price))
        if not Decimal("0") < execution_price < Decimal("1"):
            raise ValueError(f"Harga eksekusi tidak valid: {execution_price}")
    else:
        execution_price = apply_slippage_and_spread(
            historical_mid_price=real_time_price,
            spread_bps=int(settings.SPREAD_BPS),
            slippage_bps=int(settings.SLIPPAGE_BPS),
            is_buy=True,
        )
    limits = risk_limits or {}

    market_name = market.get("market_name", market_id)
    order_uuid = uuid.uuid4()

    with _write_lock, _session_scope(db_session) as db:
        try:
            # 4. evaluate_risk_and_rules (akun dikunci agar saldo tidak dipakai dua kali)
            account = _get_account(db, for_update=True)
            is_approved, rejection_reason = evaluate_risk_and_rules(
                position_size=position_size,
                available_balance=Decimal(str(account.current_balance)),
                max_position_size=Decimal(str(limits.get("max_position_size", settings.MAX_POSITION_SIZE))),
                historical_price_available=True,
                current_market_exposure=_open_exposure(db, account, market_id),
                max_market_exposure=Decimal(str(limits.get("max_market_exposure", settings.MAX_EXPOSURE_PER_MARKET))),
                current_total_exposure=_open_exposure(db, account),
                max_total_exposure=Decimal(str(limits.get("max_total_exposure", settings.MAX_TOTAL_EXPOSURE))),
            )
            if not is_approved:
                logger.warning(f"Paper order rejected by risk control: {rejection_reason}")
                # REJECTED -> Langsung raise error, TIDAK ADA partial write ke DB!
                raise ValueError(rejection_reason)

            # 5. calculate_shares dari harga setelah slippage
            shares = calculate_shares(position_size=position_size, entry_price=execution_price)

            # 6. Simpan order + posisi + saldo dalam satu transaksi
            side_enum = TradeSide(normalized_side)
            db.add(PaperOrder(
                paper_order_id=order_uuid,
                account_id=account.id,
                market_id=market_id,
                timestamp=now,
                side=side_enum,
                entry_price=execution_price,
                position_size=position_size,
                shares=shares,
                status=PaperOrderStatus.FILLED,
                strategy_version=strategy_version,
            ))

            pos = (
                db.query(PaperPosition)
                .filter(PaperPosition.account_id == account.id,
                        PaperPosition.market_id == market_id,
                        PaperPosition.side == side_enum)
                .with_for_update()
                .first()
            )
            if pos is None:
                db.add(PaperPosition(
                    account_id=account.id,
                    market_id=market_id,
                    market_name=market_name,
                    side=side_enum,
                    shares=shares,
                    average_entry_price=execution_price,
                    position_size=position_size,
                    strategy_version=strategy_version,
                    created_at=now,
                ))
            else:
                new_shares = Decimal(str(pos.shares)) + shares
                new_size = Decimal(str(pos.position_size)) + position_size
                pos.shares = new_shares
                pos.position_size = new_size
                pos.average_entry_price = _q(new_size / new_shares)
                pos.market_name = pos.market_name or market_name

            account.current_balance = Decimal(str(account.current_balance)) - position_size
            db.flush()
            _record_balance_snapshot(db, account, now=now)
            db.commit()
        except Exception:
            db.rollback()
            raise

    logger.info(
        f"Paper order successfully created: id={order_uuid}, market={market_id}, side={normalized_side}, "
        f"entry_price=${execution_price:.4f}, shares={shares}, status=FILLED"
    )

    order_data = {
        "order_id": str(order_uuid),
        "market_id": market_id,
        "market_name": market_name,
        "side": normalized_side,
        "status": PaperOrderStatus.FILLED.value,
        "requested_price": Decimal(str(user_viewed_price)) if user_viewed_price is not None else None,
        "actual_price": real_time_price,
        "execution_price": execution_price,
        "entry_price": execution_price,
        "position_size": position_size,
        "shares": shares,
        "strategy_version": strategy_version,
        "timestamp": now.isoformat(),
        "warning": warning_message,
        "polymarket_url": get_polymarket_url(market_id, market_name),
    }
    if notify:
        _notify_async("notify_paper_buy", {
            "market": market_name,
            "side": normalized_side,
            "entry_price": execution_price,
            "position_size": position_size,
            "shares": shares,
            "expected_peak": "-",
            "reason": reason or f"Manual paper order ({strategy_version})",
            "polymarket_url": order_data["polymarket_url"],
        })
    return order_data


def sell_paper_position(
    market_id: str,
    side: str,
    shares_to_sell: Optional[Decimal] = None,
    now: Optional[datetime] = None,
    db_session: Optional[Session] = None,
) -> Dict[str, Any]:
    """
    Menjual posisi paper trading yang sedang terbuka (Paper Sell).

    1. Mencari open position berdasarkan market_id DAN side (harus cocok persis).
    2. Mengambil harga real-time terbaru; ditolak jika harga tidak tersedia, stale,
       atau market sudah resolve (posisi akan di-settle otomatis).
    3. Harga eksekusi = harga pasar setelah spread & slippage sisi jual.
    4. proceeds = shares * execution_price; cost basis proporsional terhadap average entry.
    5. Dalam satu transaksi: tambah saldo, kurangi/hapus posisi, catat trade (CLOSED).
    """
    normalized_side = _normalize_side(side)
    now = _aware(now) or _utcnow()

    with _write_lock, _session_scope(db_session) as db:
        try:
            account = _get_account(db, for_update=True)
            pos = (
                db.query(PaperPosition)
                .filter(PaperPosition.account_id == account.id,
                        PaperPosition.market_id == market_id,
                        PaperPosition.side == TradeSide(normalized_side),
                        PaperPosition.shares > 0)
                .with_for_update()
                .first()
            )
            if pos is None:
                raise ValueError(f"Posisi terbuka untuk market '{market_id}' ({normalized_side}) tidak ditemukan.")

            available_shares = Decimal(str(pos.shares))
            if shares_to_sell is None:
                shares_to_sell = available_shares
            shares_to_sell = _q(Decimal(str(shares_to_sell)))
            if shares_to_sell <= Decimal("0"):
                raise ValueError("Jumlah shares yang dijual harus lebih dari 0.")
            if shares_to_sell > available_shares + DUST_SHARES:
                raise ValueError(
                    f"Jumlah shares ({shares_to_sell}) melebihi shares yang dimiliki ({_q(available_shares)})."
                )
            # Jual semua jika sisa hanya debu pembulatan
            if available_shares - shares_to_sell <= DUST_SHARES:
                shares_to_sell = available_shares

            market = get_market_by_id(market_id, now=now)
            if db.query(MarketResolution).filter(MarketResolution.market_id == market_id).first() or (
                market and market.get("is_resolved")
            ):
                raise ValueError(
                    f"Market '{market_id}' sudah resolve. Posisi akan di-settle otomatis oleh settlement worker."
                )
            if market and str(market.get("status", "")).lower() != "open":
                raise ValueError(
                    f"Market '{market_id}' sedang tidak menerima order (status: {market.get('status')}). "
                    "Penjualan dibatalkan."
                )
            live_price = _outcome_price(market, normalized_side)
            if live_price is None or live_price <= Decimal("0"):
                raise ValueError(f"Harga real-time untuk market '{market_id}' tidak tersedia. Penjualan dibatalkan.")
            if market.get("is_stale") and settings.REJECT_STALE_ORDERS:
                raise ValueError(
                    f"REJECTED: Data harga market '{market_id}' stale. Tunggu collector memperbarui harga."
                )

            exit_price = apply_slippage_and_spread(
                historical_mid_price=live_price,
                spread_bps=int(settings.SPREAD_BPS),
                slippage_bps=int(settings.SLIPPAGE_BPS),
                is_buy=False,
            )
            avg_entry = Decimal(str(pos.average_entry_price))
            position_size = Decimal(str(pos.position_size))
            proceeds = _q(shares_to_sell * exit_price)
            if proceeds < Decimal(str(settings.MIN_ORDER_NOTIONAL)):
                raise ValueError(
                    f"Nilai penjualan ${proceeds} di bawah minimum ${settings.MIN_ORDER_NOTIONAL}."
                )
            fully_closed = shares_to_sell == available_shares
            cost_basis = position_size if fully_closed else _q(position_size * shares_to_sell / available_shares)
            realized_pnl = _q(proceeds - cost_basis)

            account.current_balance = Decimal(str(account.current_balance)) + proceeds
            account.realized_pnl = Decimal(str(account.realized_pnl)) + realized_pnl

            market_name = pos.market_name or (market or {}).get("market_name") or market_id
            strategy_version = pos.strategy_version or "manual"
            if fully_closed:
                db.delete(pos)
            else:
                pos.shares = available_shares - shares_to_sell
                pos.position_size = position_size - cost_basis

            trade = PaperTrade(
                account_id=account.id,
                market_id=market_id,
                market_name=market_name,
                side=TradeSide(normalized_side),
                entry_price=avg_entry,
                position_size=cost_basis,
                shares=shares_to_sell,
                exit_price=exit_price,
                gross_pnl=realized_pnl,
                fees=Decimal("0"),
                net_pnl=realized_pnl,
                status=PaperTradeStatus.CLOSED,
                opened_at=_aware(pos.created_at) or now,
                closed_at=now,
                strategy_version=strategy_version,
            )
            db.add(trade)
            db.flush()
            _record_balance_snapshot(db, account, trade_id=trade.id, now=now)
            db.commit()
            trade_id = str(trade.id)
            new_balance = Decimal(str(account.current_balance))
        except Exception:
            db.rollback()
            raise

    logger.info(
        f"Paper position sold: market={market_id}, side={normalized_side}, shares={shares_to_sell}, "
        f"exit_price=${exit_price:.4f}, proceeds=${proceeds:.4f}, pnl=${realized_pnl:.4f}"
    )

    return {
        "success": True,
        "trade_id": trade_id,
        "market_id": market_id,
        "market_name": market_name,
        "side": normalized_side,
        "shares_sold": float(shares_to_sell),
        "exit_price": float(exit_price),
        "proceeds": float(proceeds),
        "cost_basis": float(cost_basis),
        "realized_pnl": float(realized_pnl),
        "new_balance": float(new_balance),
        "message": (
            f"Successfully sold {shares_to_sell:.4f} shares at ${exit_price:.4f}. "
            f"Realized P/L: {'+' if realized_pnl >= 0 else '-'}${abs(realized_pnl):.4f}"
        ),
    }


def settle_resolved_positions(now: Optional[datetime] = None, db: Optional[Session] = None) -> List[Dict[str, Any]]:
    """
    Settlement otomatis: setiap posisi terbuka pada market yang sudah memiliki hasil
    (tabel market_resolutions) ditutup memakai calculate_settlement.
    - Outcome menang dibayar $1/share (dikurangi fee dari profit), kalah $0.
    - Outcome INVALID: modal dikembalikan (status CANCELLED).
    Idempotent: posisi dihapus dalam transaksi yang sama dengan pencatatan trade,
    sehingga menjalankan ulang tidak menghasilkan settlement ganda.
    """
    now = _aware(now) or _utcnow()
    results: List[Dict[str, Any]] = []
    fee_bps = int(settings.FEE_RATE_BPS)

    with _write_lock, _session_scope(db) as session:
        account = _get_account(session)
        pending = (
            session.query(PaperPosition, MarketResolution)
            .join(MarketResolution, MarketResolution.market_id == PaperPosition.market_id)
            .filter(PaperPosition.account_id == account.id, PaperPosition.shares > 0)
            .all()
        )
        for pos, resolution in pending:
            try:
                account = _get_account(session, for_update=True)
                side = _side_value(pos.side)
                shares = Decimal(str(pos.shares))
                size = Decimal(str(pos.position_size))
                winner = str(resolution.winning_outcome).upper()

                if winner == "INVALID":
                    payout, fees, net = size, Decimal("0"), Decimal("0")
                    status = PaperTradeStatus.CANCELLED
                    exit_price = _q(size / shares) if shares > 0 else Decimal("0")
                else:
                    is_winner = winner == side
                    s = calculate_settlement(shares=shares, position_size=size, is_winner=is_winner,
                                             is_resolved=True, fee_rate_bps=fee_bps)
                    payout, fees, net = s["payout"], s["fees"], s["net_profit"]
                    status = PaperTradeStatus.WON if is_winner else PaperTradeStatus.LOST
                    exit_price = Decimal("1") if is_winner else Decimal("0")

                old_balance = Decimal(str(account.current_balance))
                account.current_balance = old_balance + payout - fees
                account.realized_pnl = Decimal(str(account.realized_pnl)) + net
                account.total_fees = Decimal(str(account.total_fees)) + fees

                market_name = pos.market_name or resolution.market_name or pos.market_id
                trade = PaperTrade(
                    account_id=account.id,
                    market_id=pos.market_id,
                    market_name=market_name,
                    side=pos.side,
                    entry_price=Decimal(str(pos.average_entry_price)),
                    position_size=size,
                    shares=shares,
                    exit_price=exit_price,
                    gross_pnl=_q(net + fees),
                    fees=fees,
                    net_pnl=net,
                    status=status,
                    opened_at=_aware(pos.created_at) or now,
                    closed_at=now,
                    strategy_version=pos.strategy_version or "manual",
                )
                session.add(trade)
                session.delete(pos)
                session.flush()
                _record_balance_snapshot(session, account, trade_id=trade.id, now=now)
                session.commit()
            except Exception as err:
                session.rollback()
                logger.error("Settlement gagal untuk market %s: %s", pos.market_id, err, exc_info=True)
                continue

            result = {
                "market": market_name,
                "market_id": trade.market_id,
                "side": side,
                "result": "WIN" if status == PaperTradeStatus.WON else ("LOSS" if status == PaperTradeStatus.LOST else "REFUND"),
                "status": status.value,
                "entry_price": trade.entry_price,
                "gross_pnl": trade.gross_pnl,
                "fees": fees,
                "net_pnl": net,
                "old_balance": old_balance,
                "new_balance": Decimal(str(account.current_balance)),
                "polymarket_url": get_polymarket_url(trade.market_id, market_name),
            }
            results.append(result)
            logger.info("Posisi di-settle: %s", result)
            # Tanpa pesan "PAPER TRADE SETTLED" per posisi: hasil dirangkum di laporan per jam grup auto trade

    return results
