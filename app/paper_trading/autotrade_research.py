"""
Riset & penyesuaian auto trader dari sampel sinyal (autotrade_signals) — ditrade maupun dilewati.

1. Pelacak hasil: setelah market resolve, isi outcome (WIN/LOSS/VOID) dan PnL per share seandainya
   membeli sisi sinyal di harga saat itu (ask VWAP + fee).
2. Laporan: kalibrasi model (peluang vs kenyataan), ROI per rentang edge, per alasan dilewati, per
   menit masuk (BTC), per kota / jam menuju puncak / sepakat-tidak (cuaca), dan fill rate maker.
3. Saran: ambang edge terbaik per strategi dengan simulasi "satu entri per market" (sampel PERTAMA per
   market yang lolos ambang), hanya bila sampel cukup. Keputusan mengubah aturan tetap di pengguna.
"""
import csv
import io
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.models import AutotradeLimitOrder, AutotradeSignal, MarketResolution, PaperTrade

logger = get_logger("autotrade_research")

MIN_SAMPLE = 30                      # sampel minimum sebelum saran ambang dikeluarkan
EDGE_THRESHOLDS = [0.0, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15]
EDGE_BUCKETS = [(-1.0, 0.0, "< 0¢"), (0.0, 0.03, "0–3¢"), (0.03, 0.05, "3–5¢"), (0.05, 0.08, "5–8¢"),
                (0.08, 1.0, "≥ 8¢")]
WAIT_BY_LENGTH = {60: timedelta(minutes=70), 15: timedelta(minutes=25), 5: timedelta(minutes=12)}
WEATHER_WAIT = timedelta(hours=8)
CHECK_INTERVAL = timedelta(minutes=30)
RETENTION = timedelta(days=120)
THRESHOLD_SETTING = {"weather": "WEATHER_MIN_EDGE", "weather_post": "WEATHER_MIN_EDGE"}  # seri crypto: BTC_MIN_EDGE
# Sinyal yang HANYA terhalang oleh ambang edge (layak dipakai untuk menguji ambang lain)
EDGE_ONLY_REASONS = {None, "edge di bawah minimum"}


def _crypto_series() -> Dict[str, Dict[str, Any]]:
    from app.paper_trading.autotrader import BTC_SERIES
    return BTC_SERIES


def _wait_before_check(strategy: str) -> timedelta:
    info = _crypto_series().get(strategy)
    return WAIT_BY_LENGTH[info["minutes"]] if info else WEATHER_WAIT


def threshold_setting(strategy: str) -> Optional[str]:
    return "BTC_MIN_EDGE" if strategy in _crypto_series() else THRESHOLD_SETTING.get(strategy)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- Pelacak hasil ------------------------------------------------------------------------

def track_signal_outcomes(now: Optional[datetime] = None, limit: int = 400) -> Dict[str, int]:
    from app.market_collector.collector import sync_markets_by_condition_ids

    now = now or datetime.now(timezone.utc)
    summary = {"checked": 0, "resolved": 0, "purged": 0}
    db = get_db_session()
    try:
        summary["purged"] = db.query(AutotradeSignal).filter(AutotradeSignal.created_at < now - RETENTION).delete()
        pending = []
        for sig in (db.query(AutotradeSignal).filter(AutotradeSignal.outcome.is_(None))
                    .order_by(AutotradeSignal.created_at).limit(limit * 3)):
            wait = _wait_before_check(sig.strategy)
            checked = _aware(sig.checked_at)
            if now - _aware(sig.created_at) < wait or (checked and now - checked < CHECK_INTERVAL):
                continue
            pending.append(sig)
            if len(pending) >= limit:
                break
        if not pending:
            db.commit()
            return summary
        ids = sorted({s.market_id for s in pending})
        known = {r.market_id: r.winning_outcome
                 for r in db.query(MarketResolution).filter(MarketResolution.market_id.in_(ids))}
        missing = [m for m in ids if m not in known]
        if missing:
            try:
                sync_markets_by_condition_ids(missing)
            except Exception as err:
                logger.warning("Gagal sinkron resolusi sinyal: %s", err)
            known.update({r.market_id: r.winning_outcome
                          for r in db.query(MarketResolution).filter(MarketResolution.market_id.in_(missing))})
        for sig in pending:
            summary["checked"] += 1
            sig.checked_at = now
            winner = known.get(sig.market_id)
            if not winner:
                continue
            cost = float(sig.price or 0) + float(sig.fee or 0)
            if winner == "INVALID":
                sig.outcome, pnl = "VOID", 0.0
            elif winner == sig.side:
                sig.outcome, pnl = "WIN", 1 - cost
            else:
                sig.outcome, pnl = "LOSS", -cost
            sig.pnl_per_share = Decimal(str(round(pnl, 6)))
            sig.resolved_at = now
            summary["resolved"] += 1
        db.commit()
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def run_signal_tracking() -> int:
    """Dipanggil dari loop collector; tidak pernah melempar exception."""
    try:
        return track_signal_outcomes()["resolved"]
    except Exception as err:
        logger.error("Gagal melacak hasil sinyal: %s", err, exc_info=True)
        return 0


# --- Statistik ----------------------------------------------------------------------------

def _row(sig: AutotradeSignal) -> Dict[str, Any]:
    try:
        features = json.loads(sig.features or "{}")
    except ValueError:
        features = {}
    return {"id": sig.id, "strategy": sig.strategy, "market_id": sig.market_id, "label": sig.label, "side": sig.side,
            "prob": float(sig.model_prob or 0), "price": float(sig.price or 0), "fee": float(sig.fee or 0),
            "edge": float(sig.edge or 0), "action": sig.action, "skip_reason": sig.skip_reason,
            "outcome": sig.outcome, "pnl": float(sig.pnl_per_share) if sig.pnl_per_share is not None else None,
            "created_at": _aware(sig.created_at), "features": features}


def load_signals(days: Optional[int] = None, strategy: Optional[str] = None,
                 now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    try:
        query = db.query(AutotradeSignal)
        if days:
            query = query.filter(AutotradeSignal.created_at >= now - timedelta(days=days))
        if strategy:
            query = query.filter(AutotradeSignal.strategy == strategy)
        return [_row(s) for s in query.order_by(AutotradeSignal.created_at)]
    finally:
        db.close()


def summarize(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """n, menang, win rate, rata-rata peluang model & biaya, ROI per share (beli 1 share tiap sinyal)."""
    rows = [r for r in rows if r["outcome"] in ("WIN", "LOSS")]
    if not rows:
        return {"n": 0, "wins": 0, "win_rate": None, "avg_prob": None, "avg_cost": None, "roi": None}
    wins = sum(1 for r in rows if r["outcome"] == "WIN")
    cost = sum(r["price"] + r["fee"] for r in rows)
    pnl = sum(r["pnl"] for r in rows)
    return {"n": len(rows), "wins": wins, "win_rate": wins / len(rows),
            "avg_prob": sum(r["prob"] for r in rows) / len(rows), "avg_cost": cost / len(rows),
            "roi": pnl / cost if cost else None}


def first_per_market(rows: List[Dict[str, Any]], threshold: float) -> List[Dict[str, Any]]:
    """Simulasi 'satu entri per market': sampel pertama per market yang edge-nya ≥ ambang."""
    seen, picked = set(), []
    for r in sorted(rows, key=lambda r: r["created_at"]):
        if r["market_id"] in seen or r["edge"] < threshold:
            continue
        seen.add(r["market_id"])
        picked.append(r)
    return picked


def _group(rows, key_fn) -> List[Dict[str, Any]]:
    groups = defaultdict(list)
    for r in rows:
        k = key_fn(r)
        if k is not None:
            groups[k].append(r)
    return [{"key": k, **summarize(v)} for k, v in groups.items()]


def calibration(rows) -> List[Dict[str, Any]]:
    out = []
    for lo in range(0, 100, 10):
        bucket = [r for r in rows if lo / 100 <= r["prob"] < (lo + 10) / 100 or (lo == 90 and r["prob"] == 1)]
        s = summarize(bucket)
        if s["n"]:
            out.append({"range": f"{lo}–{lo + 10}%", **s})
    return out


def _minute_bucket(r):
    minute = r["features"].get("minute")
    info = _crypto_series().get(r["strategy"])
    step = {60: 5, 15: 2, 5: 1}.get(info["minutes"] if info else 15, 2)
    return f"menit {int(minute // step * step)}+" if minute is not None else None


def _hours_bucket(r):
    h = r["features"].get("hours_to_peak_end")
    if h is None:
        return None
    return "setelah puncak" if h <= 0 else ("≤1 jam" if h <= 1 else ("1–2 jam" if h <= 2 else "> 2 jam"))


def maker_stats(days: Optional[int] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    from app.paper_trading.autotrader import STRATEGY_VERSIONS

    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    try:
        out = {}
        for strategy in [f"maker_{name}" for name in _crypto_series()]:
            query = db.query(AutotradeLimitOrder).filter(AutotradeLimitOrder.strategy == strategy)
            if days:
                query = query.filter(AutotradeLimitOrder.created_at >= now - timedelta(days=days))
            counts = defaultdict(int)
            for o in query:
                counts[o.status] += 1
            trades = db.query(PaperTrade).filter(PaperTrade.strategy_version == STRATEGY_VERSIONS[strategy]).all()
            pnl = sum(float(t.net_pnl or 0) for t in trades)
            cost = sum(float(t.position_size or 0) for t in trades)
            placed = sum(counts.values())
            closed = counts["filled"] + counts["cancelled"] + counts["expired"]
            out[strategy] = {"placed": placed, **dict(counts), "fill_rate": counts["filled"] / closed if closed else None,
                             "settled": len(trades), "wins": sum(1 for t in trades if float(t.net_pnl or 0) > 0),
                             "pnl": round(pnl, 2), "roi": pnl / cost if cost else None}
        return out
    finally:
        db.close()


def suggestions(by_strategy: Dict[str, List[Dict[str, Any]]]) -> List[str]:
    from app.paper_trading.autotrader import cfg

    tips = []
    for strategy, rows in by_strategy.items():
        setting = threshold_setting(strategy)
        if not setting:
            continue
        eligible = [r for r in rows if r["skip_reason"] in EDGE_ONLY_REASONS and r["outcome"] in ("WIN", "LOSS")]
        results = []
        for t in EDGE_THRESHOLDS:
            s = summarize(first_per_market(eligible, t))
            if s["n"] >= MIN_SAMPLE and s["roi"] is not None:
                results.append((t, s))
        current = float(cfg(setting))
        if not results:
            n = len({r["market_id"] for r in eligible})
            tips.append(f"{strategy}: data belum cukup ({n} market selesai, butuh ≥{MIN_SAMPLE}) — belum ada saran ambang.")
            continue
        # Total untung (ROI × jumlah market) terbesar; bila seri, pertahankan ambang sekarang lalu pilih yang lebih ketat
        best_t, best = max(results, key=lambda x: (round(x[1]["roi"] * x[1]["n"], 6), round(x[1]["roi"], 6),
                                                   abs(x[0] - current) < 1e-9, x[0]))
        if best["roi"] <= 0:
            tips.append(f"{strategy}: semua ambang yang diuji ROI ≤ 0 (terbaik {best['roi'] * 100:+.1f}% di edge ≥"
                        f"{best_t * 100:.0f}¢, n={best['n']}) — pertimbangkan menonaktifkan strategi ini.")
        elif abs(best_t - current) > 1e-9:
            now_s = next((s for t, s in results if abs(t - current) < 1e-9), None)
            now_txt = f" (sekarang {current * 100:.0f}¢: ROI {now_s['roi'] * 100:+.1f}%, n={now_s['n']})" if now_s else ""
            tips.append(f"{strategy}: ambang edge {best_t * 100:.0f}¢ memberi ROI {best['roi'] * 100:+.1f}% pada "
                        f"{best['n']} market{now_txt} → pertimbangkan {setting} = {best_t:g}.")
        else:
            tips.append(f"{strategy}: ambang sekarang ({current * 100:.0f}¢) sudah terbaik — ROI {best['roi'] * 100:+.1f}%, "
                        f"n={best['n']}.")
        if strategy.startswith("weather"):
            agree = [r for r in rows if r["outcome"] in ("WIN", "LOSS") and r["features"].get("agree") is False
                     and r["skip_reason"] in ("beda dengan favorit pasar", None, "edge di bawah minimum")
                     and r["edge"] >= float(cfg("WEATHER_MIN_EDGE"))]
            s = summarize(first_per_market(agree, float(cfg("WEATHER_MIN_EDGE"))))
            if s["n"] >= MIN_SAMPLE and s["roi"] is not None and s["roi"] > 0:
                tips.append(f"{strategy}: sinyal yang BEDA dengan favorit pasar pun untung (ROI {s['roi'] * 100:+.1f}%, "
                            f"n={s['n']}) → pertimbangkan mematikan syarat 'wajib sepakat'.")
    return tips


def research_report(days: Optional[int] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    rows = load_signals(days=days, now=now)
    by_strategy: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_strategy[r["strategy"]].append(r)
    strategies = {}
    for strategy, items in by_strategy.items():
        resolved = [r for r in items if r["outcome"] in ("WIN", "LOSS")]
        strategies[strategy] = {
            "samples": len(items),
            "traded": sum(1 for r in items if r["action"] == "traded"),
            "resolved": len(resolved),
            "markets": len({r["market_id"] for r in items}),
            "all": summarize(resolved),
            "traded_stats": summarize(r for r in resolved if r["action"] == "traded"),
            "calibration": calibration(resolved),
            "edge_buckets": [{"range": label, **summarize(r for r in resolved if lo <= r["edge"] < hi)}
                             for lo, hi, label in EDGE_BUCKETS],
            "by_reason": sorted(_group(resolved, lambda r: r["skip_reason"] or "ditrade"), key=lambda g: -g["n"]),
            "by_minute": sorted(_group(resolved, _minute_bucket), key=lambda g: g["key"])
            if strategy in _crypto_series() else [],
            "by_city": sorted(_group(resolved, lambda r: r["features"].get("city")), key=lambda g: -g["n"])[:10]
            if strategy.startswith("weather") else [],
            "by_agree": _group(resolved, lambda r: {True: "sepakat", False: "beda"}.get(r["features"].get("agree")))
            if strategy.startswith("weather") else [],
            "by_hours": _group(resolved, _hours_bucket) if strategy.startswith("weather") else [],
        }
    return {"days": days, "strategies": strategies, "maker": maker_stats(days, now),
            "suggestions": suggestions(by_strategy), "min_sample": MIN_SAMPLE}


def _pct(x: Optional[float], signed: bool = False) -> str:
    if x is None:
        return "-"
    return f"{x * 100:+.1f}%" if signed else f"{x * 100:.0f}%"


def format_research(days: Optional[int] = None) -> str:
    from app.paper_trading.wallet_bot import md

    rep = research_report(days=days)
    label = f"{days} hari" if days else "semua waktu"
    lines = [f"🔬 *Riset auto trader* ({label})", ""]
    if not rep["strategies"]:
        return "\n".join(lines + ["Belum ada sampel sinyal. Sampel mulai terkumpul saat auto trader berjalan."])
    for name, st in rep["strategies"].items():
        a = st["all"]
        lines.append(f"*{md(name)}* — {st['samples']} sampel · {st['markets']} market · selesai {st['resolved']} · "
                     f"ditrade {st['traded']}")
        if a["n"]:
            lines.append(f"  semua sinyal: WR {_pct(a['win_rate'])} vs model {_pct(a['avg_prob'])} · "
                         f"ROI/share {_pct(a['roi'], True)}")
            buckets = [f"{b['range']} {_pct(b['roi'], True)} (n{b['n']})" for b in st["edge_buckets"] if b["n"]]
            if buckets:
                lines.append("  ROI per edge: " + " · ".join(buckets))
    maker = {k: v for k, v in rep["maker"].items() if v["placed"]}
    for name, m in maker.items():
        lines.append(f"*{md(name)}* — {m['placed']} limit order · fill rate {_pct(m['fill_rate'])} · "
                     f"selesai {m['settled']} · PnL {m['pnl']:+.2f} · ROI {_pct(m['roi'], True)}")
    if rep["suggestions"]:
        lines += ["", "💡 *Saran*"] + [f"• {md(t)}" for t in rep["suggestions"]]
    lines += ["", f"ROI/share = seandainya membeli 1 share di harga ask + fee saat sinyal. Saran ambang memakai "
              f"satu entri per market dan butuh ≥{rep['min_sample']} market. Detail & CSV di dashboard."]
    return "\n".join(lines)


def signals_csv(days: Optional[int] = None) -> str:
    rows = load_signals(days=days)
    feature_keys = sorted({k for r in rows for k in r["features"]})
    out = io.StringIO()
    writer = csv.writer(out)
    base = ["created_at", "strategy", "market_id", "label", "side", "prob", "price", "fee", "edge", "action",
            "skip_reason", "outcome", "pnl_per_share"]
    writer.writerow(base + feature_keys)
    for r in rows:
        writer.writerow([r["created_at"].isoformat(), r["strategy"], r["market_id"], r["label"], r["side"],
                         r["prob"], r["price"], r["fee"], r["edge"], r["action"], r["skip_reason"] or "",
                         r["outcome"] or "", "" if r["pnl"] is None else r["pnl"]]
                        + [r["features"].get(k, "") for k in feature_keys])
    return out.getvalue()
