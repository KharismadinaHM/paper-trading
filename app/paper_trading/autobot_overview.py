"""
Ringkasan gaya "profil trader" untuk page Auto Bot: PnL, win rate, volume, fee, trade terbaik/terburuk,
kurva PnL kumulatif, rincian per seri, dan posisi terbuka — untuk paper maupun live (uang asli).
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.models import LiveOrder, PaperTrade, PaperTradeStatus

logger = get_logger(__name__)

PERIODS = {"1d": timedelta(days=1), "1w": timedelta(days=7), "1m": timedelta(days=30), "all": None}
CURVE_POINTS = 160


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _paper_rows(start: datetime, end: datetime, strategy: Optional[str]) -> List[Dict[str, Any]]:
    from app.paper_trading.autotrader import STRATEGY_VERSIONS, _closed

    names = {v: k for k, v in STRATEGY_VERSIONS.items()}
    out = []
    for t in _closed(start, end, strategy):
        out.append({"t": _aware(t.closed_at), "pnl": float(t.net_pnl or 0), "cost": float(t.position_size or 0),
                    "fee": float(t.fees or 0), "strategy": names.get(t.strategy_version, t.strategy_version),
                    "market": t.market_name or t.market_id, "entry": float(t.entry_price),
                    "outcome": t.side.value if hasattr(t.side, "value") else str(t.side)})
    return out


def _live_rows(start: datetime, end: datetime, strategy: Optional[str]) -> List[Dict[str, Any]]:
    from app.paper_trading.live_trader import _live_fee, _resolved_between

    return [{"t": _aware(o.resolved_at), "pnl": float(o.pnl or 0), "cost": float(o.spent or o.usd or 0),
             "fee": _live_fee(o), "strategy": o.strategy, "market": o.title or o.market_id,
             "entry": float(o.avg_price or o.max_price), "outcome": o.outcome}
            for o in _resolved_between(start, end, strategy)]


def _open_positions(source: str, strategy: Optional[str]) -> Dict[str, Any]:
    db = get_db_session()
    try:
        if source == "live":
            q = db.query(LiveOrder).filter(LiveOrder.status == "filled", LiveOrder.result.is_(None))
            if strategy in ("btc_all", "eth_all"):
                from app.paper_trading.autotrader import BTC_SERIES
                q = q.filter(LiveOrder.strategy.in_([n for n, i in BTC_SERIES.items()
                                                     if i["asset"] == strategy.split("_")[0]]))
            elif strategy:
                q = q.filter(LiveOrder.strategy == strategy)
            rows = q.all()
            return {"count": len(rows), "value": round(sum(float(o.spent or o.usd or 0) for o in rows), 2)}
        from app.paper_trading.autotrader import _versions_for
        rows = (db.query(PaperTrade).filter(PaperTrade.strategy_version.in_(_versions_for(strategy)),
                                            PaperTrade.status == PaperTradeStatus.OPEN).all())
        return {"count": len(rows), "value": round(sum(float(t.position_size or 0) for t in rows), 2)}
    finally:
        db.close()


def _curve(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """PnL kumulatif per trade selesai, di-sampling agar maksimal CURVE_POINTS titik (titik terakhir selalu ada)."""
    points, total = [], 0.0
    for r in rows:
        total += r["pnl"]
        points.append({"t": r["t"].isoformat(), "pnl": round(total, 2)})
    if len(points) > CURVE_POINTS:
        step = len(points) / CURVE_POINTS
        points = [points[int(i * step)] for i in range(CURVE_POINTS - 1)] + [points[-1]]
    return points


def _trade_card(r: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if r is None:
        return None
    return {"market": r["market"], "strategy": r["strategy"], "outcome": r["outcome"], "pnl": round(r["pnl"], 2),
            "roi": r["pnl"] / r["cost"] if r["cost"] else None, "entry": r["entry"], "closed_at": r["t"].isoformat()}


def _live_balance() -> Optional[float]:
    """Saldo USDC wallet bot (None bila live tidak dikonfigurasi atau gagal dicek)."""
    from app.core.config import settings
    from app.paper_trading import live_trader

    if not settings.LIVE_TRADING or live_trader.config_problems():
        return None
    try:
        return live_trader.usdc_balance()
    except Exception as err:
        logger.warning("Saldo USDC gagal dicek: %s", live_trader.redact(err)[:120])
        return None


def overview(source: str = "paper", period: str = "all", strategy: Optional[str] = None,
             now: Optional[datetime] = None) -> Dict[str, Any]:
    if source not in ("paper", "live"):
        raise ValueError("source: paper atau live")
    if period not in PERIODS:
        raise ValueError("period: 1d, 1w, 1m, atau all")
    now = now or datetime.now(timezone.utc)
    span = PERIODS[period]
    start = now - span if span else datetime(2020, 1, 1, tzinfo=timezone.utc)
    rows = (_live_rows if source == "live" else _paper_rows)(start, now + timedelta(seconds=1), strategy)
    rows.sort(key=lambda r: r["t"])
    wins = sum(1 for r in rows if r["pnl"] > 0)
    losses = sum(1 for r in rows if r["pnl"] < 0)
    pnl = sum(r["pnl"] for r in rows)
    volume = sum(r["cost"] for r in rows)
    by_strategy: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        b = by_strategy.setdefault(r["strategy"], {"trades": 0, "wins": 0, "pnl": 0.0, "cost": 0.0})
        b["trades"] += 1
        b["wins"] += r["pnl"] > 0
        b["pnl"] += r["pnl"]
        b["cost"] += r["cost"]
    series = sorted(({"strategy": k, "trades": b["trades"], "wins": b["wins"], "win_rate": b["wins"] / b["trades"],
                      "pnl": round(b["pnl"], 2), "roi": b["pnl"] / b["cost"] if b["cost"] else None}
                     for k, b in by_strategy.items()), key=lambda x: -x["pnl"])
    best = max(rows, key=lambda r: r["pnl"], default=None)
    worst = min(rows, key=lambda r: r["pnl"], default=None)
    return {
        "source": source, "period": period, "strategy": strategy,
        "pnl": round(pnl, 2), "trades": len(rows), "wins": wins, "losses": losses,
        "win_rate": wins / len(rows) if rows else None, "volume": round(volume, 2),
        "fees": round(sum(r["fee"] for r in rows), 2), "fees_estimated": source == "live",
        "roi": pnl / volume if volume else None,
        "avg_entry": sum(r["entry"] for r in rows) / len(rows) if rows else None,
        "best": _trade_card(best if best and best["pnl"] > 0 else None),
        "worst": _trade_card(worst if worst and worst["pnl"] < 0 else None),
        "curve": _curve(rows), "series": series, "open": _open_positions(source, strategy),
        "balance": _live_balance() if source == "live" else None,
    }
