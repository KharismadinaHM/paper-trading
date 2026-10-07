"""
Verdict paper trading crypto: apakah sebuah seri layak dicoba dengan uang asli?

Dinilai dari trade paper yang SELESAI sejak awal periode statistik (/autostats reset; atau `days` terakhir):
1. jumlah trade ≥ MIN_TRADES (total) / MIN_TRADES_SERIES (per seri) dan rentang ≥ MIN_DAYS hari;
2. ROI positif setelah fee & slippage;
3. ROI tetap positif tanpa TOP_EXCLUDED kemenangan terbesar (bukan dari "tiket lotre");
4. paruh pertama & kedua (urut waktu selesai) sama-sama positif;
5. dinilai per seri, plus gabungan.

Hasil: LULUS (semua kriteria), GAGAL (cukup data tapi ROI ≤ 0, atau rugi jelas secara statistik ≤ −2 SE), atau
BELUM (data belum cukup / hasil campuran). z = ROI rata-rata per trade ÷ simpangan baku-nya (≈ 2 = meyakinkan).
"""
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.core.database import get_db_session
from app.paper_trading.models import PaperTrade

MIN_TRADES = 400
MIN_TRADES_SERIES = 150
MIN_DAYS = 7
TOP_EXCLUDED = 5


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _roi(trades: List[PaperTrade]) -> Optional[float]:
    cost = sum(float(t.position_size or 0) for t in trades)
    return sum(float(t.net_pnl or 0) for t in trades) / cost if cost else None


def evaluate(trades: List[PaperTrade], min_trades: int) -> Dict[str, Any]:
    trades = sorted(trades, key=lambda t: _aware(t.closed_at))
    n = len(trades)
    out: Dict[str, Any] = {"n": n, "min_trades": min_trades}
    if not n:
        return {**out, "verdict": "BELUM", "reasons": ["belum ada trade selesai"], "checks": {}}
    returns = [float(t.net_pnl or 0) / float(t.position_size) for t in trades if float(t.position_size or 0)]
    mean = sum(returns) / len(returns)
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)) if len(returns) > 1 else None
    z = mean / (sd / math.sqrt(len(returns))) if sd else None
    roi = _roi(trades)
    top = sorted(trades, key=lambda t: float(t.net_pnl or 0), reverse=True)[:TOP_EXCLUDED]
    rest = [t for t in trades if t not in top]
    roi_rest = _roi(rest) if rest else None
    half = n // 2
    roi_a, roi_b = (_roi(trades[:half]), _roi(trades[half:])) if n >= 2 else (None, None)
    days = (_aware(trades[-1].closed_at) - _aware(trades[0].closed_at)).total_seconds() / 86400
    wins = sum(1 for t in trades if float(t.net_pnl or 0) > 0)
    checks = {
        "jumlah trade": n >= min_trades,
        f"rentang ≥ {MIN_DAYS} hari": days >= MIN_DAYS,
        "ROI positif": roi is not None and roi > 0,
        f"positif tanpa {TOP_EXCLUDED} menang terbesar": roi_rest is not None and roi_rest > 0,
        "kedua paruh positif": roi_a is not None and roi_b is not None and roi_a > 0 and roi_b > 0,
    }
    enough = checks["jumlah trade"] and checks[f"rentang ≥ {MIN_DAYS} hari"]
    if z is not None and z <= -2 and n >= MIN_TRADES_SERIES:
        verdict, reasons = "GAGAL", [f"rugi secara meyakinkan (z = {z:.1f})"]
    elif enough and all(checks.values()):
        verdict, reasons = "LULUS", ["semua kriteria terpenuhi"]
    elif enough and not checks["ROI positif"]:
        verdict, reasons = "GAGAL", ["data cukup tapi ROI tidak positif"]
    else:
        verdict = "BELUM"
        reasons = [name for name, ok in checks.items() if not ok]
    return {**out, "verdict": verdict, "reasons": reasons, "checks": checks, "wins": wins,
            "win_rate": wins / n, "roi": roi, "roi_without_top": roi_rest, "roi_first_half": roi_a,
            "roi_second_half": roi_b, "z": z, "days": round(days, 1),
            "pnl": round(sum(float(t.net_pnl or 0) for t in trades), 2)}


def verdict(days: Optional[int] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    from app.paper_trading.autotrader import BTC_SERIES, STRATEGY_VERSIONS, stats_label, stats_since

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days) if days else stats_since()
    names = [n for n in STRATEGY_VERSIONS if n.replace("maker_", "") in BTC_SERIES]
    versions = {STRATEGY_VERSIONS[n]: n for n in names}
    db = get_db_session()
    try:
        q = db.query(PaperTrade).filter(PaperTrade.strategy_version.in_(list(versions)), PaperTrade.closed_at.isnot(None))
        if since:
            q = q.filter(PaperTrade.closed_at >= since)
        trades = q.all()
    finally:
        db.close()
    by: Dict[str, List[PaperTrade]] = {}
    for t in trades:
        by.setdefault(versions[t.strategy_version], []).append(t)
    series = {name: evaluate(rows, MIN_TRADES_SERIES) for name, rows in sorted(by.items())}
    return {"label": stats_label(days), "since": since.isoformat() if since else None,
            "total": evaluate(trades, MIN_TRADES), "series": series,
            "criteria": {"min_trades": MIN_TRADES, "min_trades_series": MIN_TRADES_SERIES, "min_days": MIN_DAYS,
                         "top_excluded": TOP_EXCLUDED}}


def _pct(v: Optional[float]) -> str:
    return "-" if v is None else f"{v * 100:+.1f}%"


def format_verdict(days: Optional[int] = None) -> str:
    from app.paper_trading.autotrader import strategy_header
    from app.paper_trading.wallet_bot import md

    data = verdict(days)
    icons = {"LULUS": "✅", "GAGAL": "❌", "BELUM": "⏳"}

    def block(title: str, r: Dict[str, Any]) -> List[str]:
        if not r["n"]:
            return [f"{icons['BELUM']} *{title}*: belum ada trade"]
        z = f" · z {r['z']:+.1f}" if r.get("z") is not None else ""
        lines = [f"{icons[r['verdict']]} *{title}: {r['verdict']}* — {r['n']}/{r['min_trades']} trade · {r['days']} hari",
                 f"   ROI {_pct(r['roi'])} · tanpa {TOP_EXCLUDED} terbesar {_pct(r['roi_without_top'])} · "
                 f"paruh 1/2 {_pct(r['roi_first_half'])} / {_pct(r['roi_second_half'])} · WR {r['win_rate'] * 100:.0f}%{z}"]
        if r["verdict"] != "LULUS":
            lines.append("   " + md("; ".join(r["reasons"])))
        return lines

    c = data["criteria"]
    lines = [f"🧪 *Verdict paper → live* ({data['label']})",
             f"Kriteria: ≥ {c['min_trades']} trade total (≥ {c['min_trades_series']} per seri) · ≥ {c['min_days']} hari · "
             f"ROI positif · tetap positif tanpa {c['top_excluded']} menang terbesar · kedua paruh positif", ""]
    lines += block("Gabungan crypto", data["total"])
    for name, r in data["series"].items():
        lines += block(f"{strategy_header(name)} ({md(name)})", r)
    lines += ["", "LULUS = boleh dicoba live dengan nominal kecil. Ini statistik, bukan jaminan & bukan saran finansial.",
              "`/autoverdict 14` untuk 14 hari terakhir · `/autostats reset` memulai periode baru"]
    return "\n".join(lines)

