"""
Bot auto Hong Kong (paper): market suhu tertinggi (hk_max) dan terendah (hk_min) hari ini di HKO.

Peluang tiap bracket dihitung dari:
- angka yang SUDAH terukur hari ini (max tidak bisa turun, min tidak bisa naik — hari kalender HKT);
- proyeksi per jam sisa hari (Open-Meteo dikoreksi bacaan HKO, bias meluruh — hko_hourly.project_ahead);
- angka prakiraan resmi HKO bila disebut (Local Weather Forecast), dirata-rata dengan bobot;
- ketidakpastian dari error historis proyeksi per jarak jam (tabel station_forecasts vs bacaan), dengan
  batas bawah agar tidak terlalu yakin.

Suhu akhir = max(terukur, puncak sisa hari) untuk max, min(terukur, lembah sisa hari) untuk min; puncak/lembah
sisa hari ~ Normal(mu, sigma). Bracket "X°C" = [X, X+1) karena HKO melapor 0.1°C dan bracket dibulatkan ke bawah.
Peluang yang dipakai untuk edge dicampur dengan harga pasar (HK_MODEL_WEIGHT), seperti pelajaran BTC: model
sendirian cenderung terlalu yakin.

Kerangka pola harian (min dekat matahari terbit, max 13:00–15:30, rentang 3–6°C) ada di
knowledge/hk_karakteristik.md; di sini dipakai sebagai batas wajar, angkanya tetap dari data.
"""
import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.hko_alerts import HKT, STATION, _aware

logger = get_logger("hk_bot")

HK_STRATEGIES = {"hk_max": "highest", "hk_min": "lowest"}
KIND_LABEL = {"highest": "max", "lowest": "min"}
SIGMA_FLOOR = 0.3          # °C — batas bawah ketidakpastian sebelum hari dianggap final
SIGMA_FINAL = 0.15         # °C — setelah max dianggap final (sisa hari praktis tak menambah)
SIGMA_DEFAULT = {1: 0.5, 2: 0.6, 3: 0.7, 4: 0.8, 5: 0.9, 6: 1.0}
SIGMA_PER_HOUR_BEYOND = 0.1
SIGMA_CAP = 2.5
SIGNAL_BUCKET_MINUTES = 30
_sigma_cache: Dict[str, Any] = {"at": 0.0, "values": None}


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# --- Ketidakpastian dari error historis -------------------------------------------------

def lead_errors(days: int = 30, now: Optional[datetime] = None) -> Dict[int, Tuple[float, int]]:
    """RMSE proyeksi tersimpan vs bacaan HKO per jarak jam (1–6): {lead: (rmse, n)}."""
    from app.paper_trading.hko_hourly import _reading_near
    from app.paper_trading.models import StationForecast, StationReading

    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    db = get_db_session()
    try:
        forecasts = (db.query(StationForecast)
                     .filter(StationForecast.station == STATION, StationForecast.target_at >= start,
                             StationForecast.target_at <= now).all())
        readings = [(_aware(r.observed_at), float(r.temp)) for r in
                    db.query(StationReading).filter(StationReading.station == STATION,
                                                    StationReading.observed_at >= start - timedelta(hours=1))
                    .order_by(StationReading.observed_at).all()]
    finally:
        db.close()
    if not forecasts or not readings:
        return {}
    import bisect
    times = [t for t, _ in readings]
    sums: Dict[int, List[float]] = {}
    for f in forecasts:
        target = _aware(f.target_at)
        i = bisect.bisect_left(times, target)
        near = readings[max(0, i - 1):i + 1]
        real = _reading_near(near, target)
        if real is None:
            continue
        lead = round((target - _aware(f.made_at)).total_seconds() / 3600)
        if 1 <= lead <= 6:
            sums.setdefault(lead, []).append((real - float(f.value)) ** 2)
    return {lead: (math.sqrt(sum(v) / len(v)), len(v)) for lead, v in sums.items() if v}


def sigma_for(lead_hours: float, now: Optional[datetime] = None) -> float:
    """Ketidakpastian (°C) puncak/lembah sisa hari yang terjadi lead_hours lagi."""
    if _sigma_cache["values"] is None or time.monotonic() - _sigma_cache["at"] > 3600:
        try:
            measured = lead_errors(now=now)
        except Exception as err:
            logger.warning("Gagal menghitung error historis proyeksi HKO: %s", err)
            measured = {}
        table = dict(SIGMA_DEFAULT)
        for lead, (rmse, n) in measured.items():
            if n >= 30:  # cukup sampel: pakai yang terukur, tidak lebih kecil dari separuh default
                table[lead] = max(rmse, SIGMA_DEFAULT[lead] * 0.5)
        _sigma_cache.update(at=time.monotonic(), values=table)
    table = _sigma_cache["values"]
    if lead_hours <= 1:
        value = table[1]
    elif lead_hours <= 6:
        lo = int(math.floor(lead_hours))
        hi = min(lo + 1, 6)
        value = table[lo] + (table[hi] - table[lo]) * (lead_hours - lo)
    else:
        value = table[6] + SIGMA_PER_HOUR_BEYOND * (lead_hours - 6)
    return min(max(value, SIGMA_FLOOR), SIGMA_CAP)


# --- Distribusi suhu akhir --------------------------------------------------------------

def final_cdf(x: float, kind: str, observed: Optional[float], mu: float, sigma: float) -> float:
    """P(suhu akhir < x). max akhir = max(terukur, X); min akhir = min(terukur, X); X ~ N(mu, sigma)."""
    if x == math.inf:
        return 1.0
    if x == -math.inf:
        return 0.0
    p = phi((x - mu) / sigma)
    if observed is None:
        return p
    if kind == "highest":
        return 0.0 if x <= observed else p
    return 1.0 if x > observed else p


def bracket_prob(bracket: Tuple[float, float], kind: str, observed: Optional[float], mu: float, sigma: float) -> float:
    """Bracket (lo, hi) bulat; 'X°C' = [X, X+1) di data HKO 0.1°C."""
    lo, hi = bracket
    a = lo
    b = hi + 1 if hi != math.inf else math.inf
    return max(0.0, final_cdf(b, kind, observed, mu, sigma) - final_cdf(a, kind, observed, mu, sigma))


def _projection(now: datetime) -> List[Tuple[datetime, float]]:
    """Proyeksi per jam dari jam berikutnya s/d tengah malam HKT."""
    from app.paper_trading.hko_hourly import _model_range, _readings, project_ahead

    local = now.astimezone(HKT)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=HKT)
    hours = max(0, math.ceil((midnight - now).total_seconds() / 3600))
    if hours == 0:
        return []
    db = get_db_session()
    try:
        readings = _readings(db, now - timedelta(hours=3), now + timedelta(minutes=1))
    finally:
        db.close()
    model = _model_range(now, midnight + timedelta(hours=1))
    return [(ts, v) for ts, v in project_ahead(now, readings, model, hours=hours) if ts < midnight]


def estimate(kind: str, now: datetime, status: Dict[str, Any],
             projection: Optional[List[Tuple[datetime, float]]] = None) -> Dict[str, Any]:
    """Puncak (max) / lembah (min) sisa hari: mu, sigma, dan dari mana angkanya."""
    from app.paper_trading.hko_alerts import can_be_final

    projection = _projection(now) if projection is None else projection
    observed = status["max"] if kind == "highest" else status["min"]
    local = now.astimezone(HKT)
    if projection:
        pick = max if kind == "highest" else min
        at, value = pick(projection, key=lambda p: p[1])
        lead = max((at - now).total_seconds() / 3600, 0.25)
        source = f"proyeksi {'puncak' if kind == 'highest' else 'lembah'} {at.astimezone(HKT):%H:%M}"
    else:
        fallback = status.get("estimate") if kind == "highest" else status.get("min_estimate")
        value = fallback if fallback is not None else status["temp"]
        at, lead, source = None, 3.0, "perkiraan HKO/Open-Meteo (proyeksi tidak tersedia)"
    hint = (status.get("official") or {}).get("max_hint" if kind == "highest" else "min_hint")
    w = float(settings.AUTOTRADE_HK_OFFICIAL_WEIGHT)
    use_hint = hint is not None and (local.hour < 15 if kind == "highest" else True)
    mu_raw = (1 - w) * value + w * float(hint) if use_hint else value
    from app.paper_trading.hk_calibration import bias_for
    bias = bias_for(KIND_LABEL[kind], local.hour + local.minute / 60)  # koreksi bias historis (kalibrasi harian)
    mu = mu_raw + bias
    sigma = sigma_for(lead, now)
    final = kind == "highest" and can_be_final(now, status.get("temp"), observed) and local.hour >= 15
    if final:
        sigma = SIGMA_FINAL
    return {"kind": kind, "observed": observed, "mu": round(mu, 2), "mu_raw": round(mu_raw, 2), "bias": round(bias, 2),
            "sigma": round(sigma, 2), "lead": round(lead, 2),
            "at": at.astimezone(HKT).isoformat() if at else None, "projected": round(value, 2),
            "official_hint": hint if use_hint else None, "final": final, "source": source}


def _bracket_key(label: str) -> Optional[Tuple[float, float]]:
    from app.paper_trading.hko_alerts import _parse_bracket
    return _parse_bracket(label)


def _market_prob(m: Dict[str, Any]) -> Optional[float]:
    bid, ask = m.get("bid"), m.get("ask")
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return m.get("price_yes")


def analyze(now: Optional[datetime] = None, status: Optional[Dict[str, Any]] = None,
            projection: Optional[List[Tuple[datetime, float]]] = None) -> Optional[Dict[str, Any]]:
    """Distribusi model untuk max & min hari ini + harga market per bracket. None bila HKO belum ada data."""
    from app.paper_trading.hko_alerts import hko_status

    now = now or datetime.now(timezone.utc)
    status = status or hko_status(now=now)
    if not status:
        return None
    from app.paper_trading.autotrader import cfg

    projection = _projection(now) if projection is None else projection
    from app.paper_trading.hk_calibration import model_weight
    weight = model_weight()
    out: Dict[str, Any] = {"now": now.astimezone(HKT).isoformat(), "temp": status["temp"], "model_weight": weight,
                           "observed_at": status["observed_at"].isoformat() if status.get("observed_at") else None,
                           "projection": [{"at": ts.astimezone(HKT).strftime("%H:%M"), "value": v} for ts, v in projection],
                           "official": (status.get("official") or {}).get("text")}
    for kind, market_key in (("highest", "market"), ("lowest", "min_market")):
        est = estimate(kind, now, status, projection)
        rows = []
        for m in status.get(market_key) or []:
            b = _bracket_key(m.get("bracket") or "")
            if b is None:
                continue
            model = bracket_prob(b, kind, est["observed"], est["mu"], est["sigma"])
            market = _market_prob(m)
            blended = model if market is None else min(max(market + weight * (model - market), 0.0), 1.0)
            rows.append({**m, "lo": b[0], "hi": b[1], "model": round(model, 4),
                         "market_prob": round(market, 4) if market is not None else None, "prob": round(blended, 4)})
        if not rows:  # tanpa market: bracket bulat di sekitar mu (untuk tampilan & AI)
            center = math.floor(est["mu"])
            for x in range(center - 3, center + 4):
                p = bracket_prob((x, x), kind, est["observed"], est["mu"], est["sigma"])
                rows.append({"bracket": f"{x}°C", "lo": x, "hi": x, "model": round(p, 4), "market_prob": None,
                             "prob": round(p, 4)})
        rows.sort(key=lambda r: (r["lo"] if r["lo"] != -math.inf else -999))
        out[KIND_LABEL[kind]] = {**est, "brackets": rows}
    return out


# --- Keputusan & eksekusi ---------------------------------------------------------------

def evaluate(strategy: str, now: datetime, analysis: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Keputusan untuk satu strategi HK dengan 'skip_reason' (None = layak dibeli)."""
    from app.paper_trading.autotrader import _book_side, already_decided, cfg

    kind = HK_STRATEGIES[strategy]
    dist = analysis[KIND_LABEL[kind]]
    local = now.astimezone(HKT)
    key = f"{strategy}|{local.date().isoformat()}"
    tradable = [r for r in dist["brackets"] if r.get("yes_token_id") and r.get("ask") is not None]
    if not tradable:
        return None
    est_fee = lambda p: settings.AUTOTRADE_FEE_RATE * p * (1 - p)  # noqa: E731
    target = max(tradable, key=lambda r: r["prob"] - r["ask"] - est_fee(r["ask"]))
    usd = float(cfg("ORDER_USD"))
    book = _book_side(target["yes_token_id"], usd)
    price = book["price"] if book else target["ask"]
    fee = book["fee"] if book else est_fee(target["ask"])
    edge = target["prob"] - (price + fee)
    if local.hour + local.minute / 60 < float(cfg("HK_START_HOUR")):
        reason = f"sebelum jam {float(cfg('HK_START_HOUR')):g}:00 HKT"
    elif already_decided(key):
        reason = "sudah trade hari ini"
    elif not book:
        reason = "order book tidak tersedia"
    elif price < float(cfg("HK_MIN_PRICE")):
        reason = f"di bawah harga min {float(cfg('HK_MIN_PRICE')) * 100:.0f}¢"
    elif price > float(cfg("MAX_PRICE")):
        reason = "harga di atas maksimum"
    elif book.get("spread") is not None and book["spread"] > float(cfg("MAX_SPREAD")):
        reason = "spread terlalu lebar"
    elif edge < float(cfg("HK_MIN_EDGE")):
        reason = f"edge < {float(cfg('HK_MIN_EDGE')) * 100:.0f}¢"
    else:
        reason = None
    label = KIND_LABEL[kind]
    detail = (f"HKO {label} terukur {dist['observed']:.1f}°C · {dist['source']} {dist['projected']:.1f}°C · "
              f"model {dist['mu']:.1f}±{dist['sigma']:.1f}°C"
              + (f" (koreksi bias {dist['bias']:+.1f})" if abs(dist.get("bias") or 0) >= 0.05 else "")
              + (f" · resmi HKO {dist['official_hint']:g}°C" if dist.get("official_hint") is not None else "")
              + (" · max dianggap final" if dist.get("final") else "")
              + f" · peluang model {target['model'] * 100:.0f}% vs pasar "
              + (f"{target['market_prob'] * 100:.0f}%" if target.get("market_prob") is not None else "-"))
    return {
        "key": key, "strategy": strategy, "market_id": target["market_id"],
        "title": f"🇭🇰 Hong Kong {label} {local.date().isoformat()} · {target['bracket']}",
        "label": f"Hong Kong {kind} {local.date().isoformat()} · {target['bracket']}",
        "side": "YES", "outcome": "YES", "prob": target["prob"], "price": price, "fee": fee, "edge": edge,
        "size": usd, "detail": detail, "skip_reason": reason,
        "features": {
            "kind": kind, "bracket": target["bracket"], "observed": dist["observed"], "mu": dist["mu"],
            "mu_raw": dist.get("mu_raw", dist["mu"]), "bias": dist.get("bias", 0.0),
            "sigma": dist["sigma"], "lead": dist["lead"], "projected": dist["projected"],
            "official_hint": dist.get("official_hint"), "final": dist.get("final"), "model_raw": target["model"],
            "market_prob": target.get("market_prob"), "model_weight": analysis.get("model_weight"),
            "temp": analysis.get("temp"), "hour_hkt": round(local.hour + local.minute / 60, 2),
            "spread": book.get("spread") if book else None, "slippage": book.get("slippage") if book else None,
        },
    }


def hk_tick(now: Optional[datetime] = None, strategies: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Satu siklus: evaluasi hk_max & hk_min, catat sinyal (juga yang nonaktif, sebagai shadow), eksekusi."""
    from app.paper_trading.autotrader import enabled_strategies, execute, log_signal

    now = now or datetime.now(timezone.utc)
    active = enabled_strategies() if strategies is None else strategies
    analysis = analyze(now)
    if not analysis:
        return []
    made = []
    bucket = int(now.timestamp() // (SIGNAL_BUCKET_MINUTES * 60))
    for strategy in HK_STRATEGIES:
        try:
            decision = evaluate(strategy, now, analysis)
        except Exception as err:
            logger.warning("Gagal mengevaluasi %s: %s", strategy, err)
            continue
        if not decision:
            continue
        skip = decision["skip_reason"] if strategy in active else (decision["skip_reason"] or "strategi nonaktif")
        log_signal(decision, f"{decision['key']}|{decision['features']['bracket']}|{bucket}", skip, now)
        if strategy in active and decision["skip_reason"] is None:
            if execute(decision, now):
                made.append(decision)
    return made


def positions_today(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Posisi paper terbuka di market suhu Hong Kong (untuk konteks AI & tampilan)."""
    from app.paper_service import get_open_positions

    out = []
    for p in get_open_positions():
        name = str(p.get("market_name") or "")
        if "hong kong" in name.lower():
            out.append({"market": name, "side": str(p.get("side")), "shares": float(p.get("shares") or 0),
                        "entry": float(p.get("entry_price") or 0), "strategy": p.get("strategy_version")})
    return out


def format_analysis_lines(analysis: Dict[str, Any], top: int = 4) -> List[str]:
    """Baris ringkas model vs pasar untuk Telegram."""
    lines = []
    for label, title in (("max", "Max"), ("min", "Min")):
        d = analysis.get(label)
        if not d:
            continue
        lines.append(f"{title}: terukur {d['observed']:.1f}°C · model {d['mu']:.1f}±{d['sigma']:.1f}°C"
                     + (" (final)" if d.get("final") else ""))
        ranked = sorted(d["brackets"], key=lambda r: -r["model"])[:top]
        lines.append("   " + " · ".join(
            f"{r['bracket']} {r['model'] * 100:.0f}%"
            + (f" (pasar {r['market_prob'] * 100:.0f}%)" if r.get("market_prob") is not None else "")
            for r in ranked))
    return lines


def today_hkt(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(HKT).date()
