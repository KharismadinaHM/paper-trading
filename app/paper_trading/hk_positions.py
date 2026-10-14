"""
Manajemen posisi bot Hong Kong: jual (exit) posisi yang nilainya di pasar sudah melebihi peluangnya menurut model,
lalu bot boleh masuk lagi ke bracket lain lewat aturan beli biasa ("switch", mis. pegang 30°C → lonjakan → jual 30,
beli 31 bila 31 lolos aturan beli).

Tiap siklus, untuk posisi paper HK yang terbuka (hk_max, hk_min, hk_max_no, hk_min_no) pada market hari ini:
- nilai jual = VWAP bid order book TERKINI untuk seluruh share − fee taker − slippage (sisi NO: 1 − ask YES);
- peluang menahan = peluang campuran model+pasar sisi kita (NO pada bracket mustahil = 100%: tak pernah dijual);
- exit bila nilai jual − peluang menahan ≥ HK_EXIT_EDGE, posisi sudah ≥ HK_SWITCH_COOLDOWN_MIN menit, dan exit hari itu
  < HK_MAX_SWITCHES.

Mode bayangan (HK_SWITCH_LIVE = false, default): hanya dicatat & dikabarkan "akan jual"; setelah market selesai dinilai:
holding ternyata lebih baik (salah langkah) atau exit menyelamatkan. HK_SWITCH_LIVE = true: benar-benar dijual (paper),
lalu strategi itu boleh membeli lagi hari itu (bukan bracket yang baru dijual).
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.hko_alerts import HKT

logger = get_logger("hk_positions")

EXIT_STRATEGY = "hk_exit"
VERSION_TO_STRATEGY = {"auto_hk_max_v1": "hk_max", "auto_hk_min_v1": "hk_min",
                       "auto_hk_max_no_v1": "hk_max_no", "auto_hk_min_no_v1": "hk_min_no"}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def exits_today(strategy: str, now: datetime) -> Tuple[int, Set[str], Optional[datetime]]:
    """(jumlah exit LIVE hari ini, market yang sudah dijual, waktu exit terakhir) untuk satu strategi HK."""
    from app.paper_trading.autotrader import _local_day
    from app.paper_trading.models import AutotradeDecision

    db = get_db_session()
    try:
        rows = (db.query(AutotradeDecision).filter(AutotradeDecision.strategy == EXIT_STRATEGY,
                                                   AutotradeDecision.status == "filled",
                                                   AutotradeDecision.local_day == _local_day(now)).all())
    finally:
        db.close()
    mine = []
    for r in rows:
        try:
            if json.loads(r.features or "{}").get("strategy") == strategy:
                mine.append(r)
        except ValueError:
            continue
    last = max((_aware(r.created_at) for r in mine), default=None)
    return len(mine), {r.market_id for r in mine}, last


def entry_key(strategy: str, now: datetime) -> Tuple[str, Set[str]]:
    """Kunci keputusan beli: satu per hari, ditambah satu lagi setelah tiap exit live. + market yang tak boleh dibeli lagi."""
    n, sold, _ = exits_today(strategy, now)
    base = f"{strategy}|{now.astimezone(HKT).date().isoformat()}"
    return (base if n == 0 else f"{base}|r{n}"), sold


def _vwap_sell(levels: List[Tuple[float, float]], shares: float) -> Optional[float]:
    """Harga rata-rata menjual `shares` ke level bid (tertinggi dulu). None bila kedalaman tidak cukup."""
    remaining, value = shares, 0.0
    for price, size in sorted(levels, reverse=True):
        take = min(size, remaining)
        value += take * price
        remaining -= take
        if remaining <= 1e-9:
            return value / shares
    return None


def sell_value(yes_token: str, side: str, shares: float) -> Optional[Dict[str, float]]:
    """Nilai jual bersih per share dari order book terkini (sisi NO memakai cermin ask YES)."""
    from app.paper_trading.autotrader import cfg, taker_fee
    from app.paper_trading.live_market_data import FRESH_BOOK_TTL, fetch_fee_rate, fetch_order_books

    book = fetch_order_books([yes_token], ttl=FRESH_BOOK_TTL).get(str(yes_token))
    if not book:
        return None
    if side == "YES":
        levels = list(book.get("bids") or [])
    else:
        levels = [(round(1 - p, 6), size) for p, size in (book.get("asks") or []) if 0 < p < 1]
    avg = _vwap_sell(levels, shares)
    if avg is None:
        return None
    fee = taker_fee(avg, fetch_fee_rate(yes_token))
    slip = float(cfg("SLIPPAGE") or 0)
    return {"bid": round(avg, 4), "fee": round(fee, 4), "net": round(max(avg - fee - slip, 0.0), 4)}


def open_positions(now: datetime) -> List[Dict[str, Any]]:
    from app.paper_trading.models import PaperPosition

    db = get_db_session()
    try:
        rows = (db.query(PaperPosition).filter(PaperPosition.strategy_version.in_(list(VERSION_TO_STRATEGY)),
                                               PaperPosition.shares > 0).all())
        return [{"market_id": p.market_id, "side": p.side.value if hasattr(p.side, "value") else str(p.side),
                 "shares": float(p.shares), "entry": float(p.average_entry_price), "size": float(p.position_size),
                 "strategy": VERSION_TO_STRATEGY[p.strategy_version], "opened_at": _aware(p.created_at),
                 "market_name": p.market_name} for p in rows]
    finally:
        db.close()


def evaluate_exit(pos: Dict[str, Any], now: datetime, analysis: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Penilaian exit satu posisi: {should_exit, reason, sell, p_hold, ...}; None bila market bukan hari ini."""
    from app.paper_trading.autotrader import cfg
    from app.paper_trading.hk_bot import KIND_LABEL, impossible_by_observation

    kind = "highest" if pos["strategy"].startswith("hk_max") else "lowest"
    dist = analysis.get(KIND_LABEL[kind]) or {}
    row = next((r for r in dist.get("brackets") or [] if r.get("market_id") == pos["market_id"]), None)
    if row is None or not row.get("yes_token_id"):
        return None
    impossible = impossible_by_observation(kind, (row["lo"], row["hi"]), dist.get("observed"))
    if pos["side"] == "YES":
        p_hold = 0.0 if impossible else row["prob"]
    else:
        p_hold = 1.0 if impossible else 1 - row["prob"]
    sell = sell_value(row["yes_token_id"], pos["side"], pos["shares"])
    edge = (sell["net"] - p_hold) if sell else None
    n, _, last_exit = exits_today(pos["strategy"], now)
    age_min = (now - pos["opened_at"]).total_seconds() / 60
    cooldown = float(cfg("HK_SWITCH_COOLDOWN_MIN"))
    if not sell:
        reason = "order book tidak cukup untuk menjual"
    elif edge < float(cfg("HK_EXIT_EDGE")):
        reason = f"nilai jual {sell['net'] * 100:.1f}¢ vs peluang menahan {p_hold * 100:.1f}%: tahan"
    elif age_min < cooldown or (last_exit and (now - last_exit).total_seconds() / 60 < cooldown):
        reason = f"jeda {cooldown:g} menit setelah beli / jual terakhir"
    elif n >= int(float(cfg("HK_MAX_SWITCHES"))):
        reason = f"sudah {n} kali exit hari ini (maks {float(cfg('HK_MAX_SWITCHES')):g})"
    else:
        reason = None
    return {"should_exit": reason is None, "reason": reason, "sell": sell, "p_hold": round(p_hold, 4),
            "edge": round(edge, 4) if edge is not None else None, "bracket": row["bracket"], "kind": kind,
            "model_yes": row.get("model"), "market_yes": row.get("market_prob"), "observed": dist.get("observed"),
            "mu": dist.get("mu"), "sigma": dist.get("sigma"), "yes_token_id": row["yes_token_id"]}


def _log(pos: Dict[str, Any], ev: Dict[str, Any], mode: str, now: datetime) -> bool:
    """Sinyal exit (sekali per posisi per hari per mode) — dinilai setelah resolve: holding vs jual."""
    from app.paper_trading.autotrader import _local_day, log_signal

    side = pos["side"]
    decision = {"strategy": EXIT_STRATEGY, "market_id": pos["market_id"],
                "label": f"Hong Kong {ev['kind']} {now.astimezone(HKT).date()} · jual {side} {ev['bracket']}",
                "side": side, "prob": ev["p_hold"], "price": ev["sell"]["net"], "fee": 0.0, "edge": ev["edge"],
                "features": {**{k: ev[k] for k in ("bracket", "kind", "p_hold", "model_yes", "market_yes", "observed",
                                                    "mu", "sigma")},
                             "strategy": pos["strategy"], "mode": mode, "shares": pos["shares"], "entry": pos["entry"],
                             "bid": ev["sell"]["bid"], "sell_net": ev["sell"]["net"]}}
    key = f"hk_exit|{mode}|{pos['market_id'][:20]}|{side}|{_local_day(now)}"
    ok = log_signal(decision, key, None, now)
    if ok:  # kolom action: shadow / sold
        from app.paper_trading.models import AutotradeSignal
        db = get_db_session()
        try:
            sig = db.query(AutotradeSignal).filter_by(signal_key=key[:255]).first()
            if sig is not None:
                sig.action = "shadow" if mode == "shadow" else "sold"
                db.commit()
        finally:
            db.close()
    return ok


def manage(now: datetime, analysis: Dict[str, Any], active: List[str]) -> List[Dict[str, Any]]:
    """Evaluasi semua posisi HK terbuka; catat bayangan atau jual (paper) bila HK_SWITCH_LIVE. Tak pernah melempar."""
    from app.paper_trading.autotrader import _record, cfg, notify

    actions = []
    if not cfg("HK_SWITCH_ENABLED"):
        return actions
    live = bool(cfg("HK_SWITCH_LIVE"))
    for pos in open_positions(now):
        try:
            ev = evaluate_exit(pos, now, analysis)
            if not ev or not ev["should_exit"]:
                continue
            gain = (ev["sell"]["net"] - pos["entry"]) * pos["shares"]
            text = (f"{ev['kind'] == 'highest' and 'MAX' or 'MIN'} {pos['side']} {ev['bracket']} · {pos['shares']:.2f} sh "
                    f"(masuk {pos['entry'] * 100:.1f}¢)\nNilai jual {ev['sell']['net'] * 100:.1f}¢ (bid {ev['sell']['bid'] * 100:.1f}¢) "
                    f"vs peluang menahan {ev['p_hold'] * 100:.0f}% → edge jual {ev['edge'] * 100:+.1f}¢ · "
                    f"HKO terukur {ev['observed']:.1f}°C · model {ev['mu']:.1f}±{ev['sigma']:.1f}°C")
            if not live:
                if _log(pos, ev, "shadow", now):
                    notify(f"👻 HK JUAL (bayangan) · {pos['strategy']}\n{text}\nPnL bila dijual sekarang {gain:+.2f} "
                           "· mode bayangan: tidak dijual (HK_SWITCH_LIVE=false)")
                    actions.append({"mode": "shadow", **ev, "market_id": pos["market_id"]})
                continue
            from app.paper_service import sell_paper_position
            try:
                result = sell_paper_position(pos["market_id"], pos["side"], now=now)
            except ValueError as err:
                logger.warning("Exit HK %s gagal: %s", pos["market_id"], err)
                continue
            n, _, _ = exits_today(pos["strategy"], now)
            decision = {"key": f"hk_exit|{pos['market_id'][:30]}|{pos['side']}|{now.astimezone(HKT).date()}|{n}",
                        "strategy": EXIT_STRATEGY, "market_id": pos["market_id"],
                        "label": f"Hong Kong {ev['kind']} · jual {pos['side']} {ev['bracket']}", "side": pos["side"],
                        "prob": ev["p_hold"], "price": ev["sell"]["net"], "fee": ev["sell"]["fee"], "edge": ev["edge"],
                        "size": round(pos["shares"] * ev["sell"]["net"], 4),
                        "features": {"strategy": pos["strategy"], "bracket": ev["bracket"], "shares": pos["shares"]}}
            _record(decision, "filled", None, now)
            _log(pos, ev, "live", now)
            pnl = result.get("realized_pnl")
            notify(f"🔁 HK JUAL (paper) · {pos['strategy']}\n{text}\nTerjual"
                   + (f" · PnL {float(pnl):+.2f}" if pnl is not None else "")
                   + "\nBot boleh membeli bracket lain hari ini bila lolos aturan beli.")
            actions.append({"mode": "live", **ev, "market_id": pos["market_id"]})
        except Exception as err:
            logger.warning("Manajemen posisi HK %s gagal: %s", pos.get("market_id"), err, exc_info=True)
    return actions


def switch_report(days: int = 30, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Penilaian exit (bayangan & live) yang sudah resolve: berapa yang menyelamatkan vs salah langkah."""
    from app.paper_trading.models import AutotradeSignal

    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    try:
        rows = (db.query(AutotradeSignal).filter(AutotradeSignal.strategy == EXIT_STRATEGY,
                                                 AutotradeSignal.created_at >= now - timedelta(days=days))
                .order_by(AutotradeSignal.created_at.desc()).all())
    finally:
        db.close()
    out: Dict[str, Any] = {"recent": [], "modes": {}}
    for r in rows:
        try:
            f = json.loads(r.features or "{}")
        except ValueError:
            f = {}
        mode = "shadow" if r.action == "shadow" else "live"
        shares = float(f.get("shares") or 0)
        item = {"created_at": _aware(r.created_at).isoformat(), "mode": mode, "label": r.label, "strategy": f.get("strategy"),
                "sell_net": float(r.price or 0), "p_hold": float(r.model_prob or 0), "outcome": r.outcome,
                # pnl_per_share sinyal = bayaran menahan − nilai jual: positif = menahan lebih baik (exit salah langkah)
                "hold_minus_sell": float(r.pnl_per_share) if r.pnl_per_share is not None else None, "shares": shares}
        out["recent"].append(item)
        m = out["modes"].setdefault(mode, {"n": 0, "resolved": 0, "saved": 0, "mistakes": 0, "net_usd": 0.0})
        m["n"] += 1
        if item["hold_minus_sell"] is not None and r.outcome in ("WIN", "LOSS"):
            m["resolved"] += 1
            m["saved" if item["hold_minus_sell"] < 0 else "mistakes"] += 1
            m["net_usd"] = round(m["net_usd"] - item["hold_minus_sell"] * shares, 2)  # + = exit menguntungkan
    out["recent"] = out["recent"][:30]
    return out
