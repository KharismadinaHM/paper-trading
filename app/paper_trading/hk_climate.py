"""
Klimatologi Hong Kong Observatory untuk page khusus HK market.

Sumber: data iklim resmi HKO (suhu max & min harian stasiun HK Observatory sejak 1884,
dataType CLMMAXT / CLMMINT) — stasiun yang sama dengan resolusi market suhu Hong Kong. Data resmi
diperbarui bulanan; hari-hari setelahnya dilengkapi dari bacaan HKO real-time yang disimpan collector
(station_readings, max/min sejak tengah malam) dan ditandai sebagai data sementara.

Bracket mengikuti aturan market HK: bacaan 0.1°C dibulatkan ke bawah (33.9 → bracket 33°C).
"""
import calendar
import csv
import io
import math
import statistics
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.logging import get_logger

logger = get_logger("hk_climate")

CLIMATE_URL = "https://data.weather.gov.hk/weatherAPI/opendata/opendata.php?dataType={kind}&rformat=csv&station=HKO"
KINDS = {"max": "CLMMAXT", "min": "CLMMINT"}
CACHE_TTL = 12 * 3600
WINDOW_DAYS = 3        # "sekitar tanggal ini" = ±3 hari
DIST_YEARS = 30        # sebaran bracket dari 30 tahun terakhir


def parse_climate_csv(text: str) -> Dict[date, float]:
    """CSV iklim HKO → {tanggal: nilai}; baris tanpa data / tidak lengkap dilewati."""
    out: Dict[date, float] = {}
    for row in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if len(row) < 4:
            continue
        try:
            y, m, d, v = int(row[0]), int(row[1]), int(row[2]), float(row[3])
        except ValueError:
            continue
        if len(row) > 4 and row[4].strip() not in ("", "C"):
            continue  # data tidak lengkap (#) / tidak tersedia
        out[date(y, m, d)] = v
    return out


def official_series(kind: str) -> Dict[date, float]:
    """Seri harian resmi HKO ('max' / 'min'), di-cache 12 jam; kosong bila gagal."""
    from app.paper_trading.live_market_data import _cached, _http

    def load():
        return parse_climate_csv(_http(CLIMATE_URL.format(kind=KINDS[kind]), timeout=60))

    try:
        return _cached(f"hk_climate:{kind}", CACHE_TTL, load)
    except Exception as err:
        logger.warning("Gagal mengambil data iklim HKO %s: %s", kind, err)
        return {}


def recorded_daily(after: Optional[date], today: date) -> Dict[str, Dict[date, float]]:
    """Max/min harian dari bacaan HKO tersimpan untuk hari setelah data resmi (termasuk hari ini, sementara)."""
    from app.core.database import get_db_session
    from app.paper_trading.hko_alerts import HKT, STATION, _aware
    from app.paper_trading.models import StationReading

    start_day = (after + timedelta(days=1)) if after else today - timedelta(days=40)
    start = datetime.combine(start_day, datetime.min.time(), tzinfo=HKT).astimezone(timezone.utc)
    out: Dict[str, Dict[date, float]] = {"max": {}, "min": {}}
    db = get_db_session()
    try:
        rows = (db.query(StationReading).filter(StationReading.station == STATION, StationReading.observed_at >= start)
                .order_by(StationReading.observed_at).all())
    except Exception as err:
        logger.warning("Gagal membaca station_readings HKO: %s", err)
        rows = []
    finally:
        db.close()
    for r in rows:
        d = _aware(r.observed_at).astimezone(HKT).date()
        hi = float(r.max_since_midnight) if r.max_since_midnight is not None else float(r.temp)
        lo = float(r.min_since_midnight) if r.min_since_midnight is not None else float(r.temp)
        out["max"][d] = max(out["max"].get(d, -math.inf), hi, float(r.temp))
        out["min"][d] = min(out["min"].get(d, math.inf), lo, float(r.temp))
    return out


def daily_series(today: date) -> Tuple[Dict[str, Dict[date, float]], Optional[date], set]:
    """({'max': {...}, 'min': {...}}, tanggal data resmi terakhir, {tanggal sementara})."""
    series = {k: dict(official_series(k)) for k in KINDS}
    last_official = max(series["max"], default=None)
    provisional = set()
    extra = recorded_daily(last_official, today)
    for kind in KINDS:
        for d, v in extra[kind].items():
            if last_official is None or d > last_official:
                series[kind][d] = round(v, 1)
                provisional.add(d)
    return series, last_official, provisional


def bracket_of(value: float) -> int:
    """Bracket market HK: dibulatkan ke bawah (33.9 → 33)."""
    return math.floor(value + 1e-9)


def _quantile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    cuts = statistics.quantiles(values, n=100, method="inclusive")
    return round(cuts[max(0, min(98, int(q * 100) - 1))], 1)


def _distribution(values: List[float]) -> List[Dict[str, Any]]:
    counts = Counter(bracket_of(v) for v in values)
    total = sum(counts.values()) or 1
    return [{"bracket": b, "count": counts[b], "share": round(counts[b] / total, 4)} for b in sorted(counts)]


def _month_days(series: Dict[date, float], year: int, month: int) -> List[Tuple[date, float]]:
    return sorted((d, v) for d, v in series.items() if d.year == year and d.month == month)


def _window(series: Dict[date, float], month: int, day: int, years: List[int]) -> List[float]:
    out = []
    for y in years:
        last = calendar.monthrange(y, month)[1]
        center = date(y, month, min(day, last))
        for k in range(-WINDOW_DAYS, WINDOW_DAYS + 1):
            v = series.get(center + timedelta(days=k))
            if v is not None:
                out.append(v)
    return out


def _years_text(hits: List[Dict[str, Any]]) -> str:
    parts = [f"{h['year']} ({h['days']} {'day' if h['days'] == 1 else 'days'})" for h in hits]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def insight_text(month_name: str, max_threshold: Optional[int], min_threshold: Optional[int],
                 max_hits: List[Dict[str, Any]], min_hits: List[Dict[str, Any]], min_warm: bool,
                 years: int, record_max: Optional[Dict[str, Any]], record_min: Optional[Dict[str, Any]]) -> str:
    """Teks gaya postingan: '<Month> has only hit a high of 34°C+ in 2 of the past 10 years ...'"""
    lines = []
    if max_threshold is not None:
        n = len(max_hits)
        if n == 0:
            rec = f" The {month_name} record is {record_max['value']:.1f}°C ({record_max['year']})." if record_max else ""
            lines.append(f"{month_name} hasn't hit a high of {max_threshold}°C+ once in the past {years} years.{rec}")
        elif n <= years / 2:
            lines.append(f"{month_name} has only hit a high of {max_threshold}°C+ in {n} of the past {years} years: "
                         f"{_years_text(max_hits)}.")
        else:
            lines.append(f"{month_name} has hit a high of {max_threshold}°C+ in {n} of the past {years} years.")
    if min_threshold is not None:
        n = len(min_hits)
        phrase = f"stayed at {min_threshold}°C or warmer" if min_warm else f"dropped to {min_threshold}°C or below"
        if n == 0:
            lines.append(f"The low hasn't {phrase} on any {month_name} day in the past {years} years.")
        else:
            lines.append(f"The {month_name} low has {phrase} in {n} of the past {years} years: {_years_text(min_hits)}.")
    lines.append("Let's see what today brings. Gm HK🌄🌡")
    return " ".join(lines)


def month_report(month: Optional[int] = None, years: int = 10, max_threshold: Optional[int] = None,
                 min_threshold: Optional[int] = None, today: Optional[date] = None) -> Dict[str, Any]:
    """
    Statistik satu bulan: per tahun (N tahun lengkap terakhir + tahun ini sejauh ini), sebaran bracket
    30 tahun, sekitar tanggal hari ini (±3 hari), rekor, dan teks insight.
    """
    from app.paper_trading.hko_alerts import HKT

    today = today or datetime.now(HKT).date()
    month = month or today.month
    series, last_official, provisional = daily_series(today)
    mx, mn = series["max"], series["min"]
    month_name = calendar.month_name[month]
    in_progress = today.month == month
    # tahun lengkap: bulan itu sudah selesai
    last_full = today.year - 1 if today.month <= month else today.year
    past = list(range(last_full - years + 1, last_full + 1))
    dist_years = list(range(last_full - DIST_YEARS + 1, last_full + 1))

    def year_row(y: int) -> Optional[Dict[str, Any]]:
        hi, lo = _month_days(mx, y, month), _month_days(mn, y, month)
        if not hi or not lo:
            return None
        top = max(hi, key=lambda r: r[1])
        bottom = min(lo, key=lambda r: r[1])
        row = {"year": y, "max": top[1], "max_date": top[0].isoformat(), "min": bottom[1],
               "min_date": bottom[0].isoformat(), "avg_max": round(statistics.mean(v for _, v in hi), 1),
               "avg_min": round(statistics.mean(v for _, v in lo), 1), "days": len(hi),
               "provisional": any(d in provisional for d, _ in hi)}
        if max_threshold is not None:
            row["max_hit_days"] = [d.day for d, v in hi if v >= max_threshold]
        return row

    rows = [r for r in (year_row(y) for y in past) if r]
    month_max = [v for y in dist_years for _, v in _month_days(mx, y, month)]
    month_min = [v for y in dist_years for _, v in _month_days(mn, y, month)]
    median_min = statistics.median(month_min) if month_min else None
    min_warm = bool(min_threshold is not None and median_min is not None and min_threshold > median_min)
    for r in rows:
        lo = _month_days(mn, r["year"], month)
        if min_threshold is not None:
            r["min_hit_days"] = [d.day for d, v in lo if (v >= min_threshold if min_warm else bracket_of(v) <= min_threshold)]
    max_hits = [{"year": r["year"], "days": len(r["max_hit_days"])} for r in rows if r.get("max_hit_days")]
    min_hits = [{"year": r["year"], "days": len(r["min_hit_days"])} for r in rows if r.get("min_hit_days")]

    all_max = [(v, d) for d, v in mx.items() if d.month == month and d not in provisional]
    all_min = [(v, d) for d, v in mn.items() if d.month == month and d not in provisional]
    record_max = (lambda r: {"value": r[0], "year": r[1].year, "date": r[1].isoformat()})(max(all_max)) if all_max else None
    record_min = (lambda r: {"value": r[0], "year": r[1].year, "date": r[1].isoformat()})(min(all_min)) if all_min else None

    target_day = today.day if in_progress else 15
    win_max = _window(mx, month, target_day, dist_years)
    win_min = _window(mn, month, target_day, dist_years)
    current = None
    if in_progress or (today.month > month and today.year == last_full + 1):
        hi, lo = _month_days(mx, today.year, month), _month_days(mn, today.year, month)
        if hi and lo:
            current = {"year": today.year, "max": max(v for _, v in hi), "min": min(v for _, v in lo), "days": len(hi),
                       "provisional_days": sum(1 for d, _ in hi if d in provisional)}
    return {
        "month": month, "month_name": month_name, "years": years, "past_years": past,
        "rows": rows, "current": current,
        "summary": {
            "avg_max": round(statistics.mean(month_max), 1) if month_max else None,
            "avg_min": round(statistics.mean(month_min), 1) if month_min else None,
            "p90_max": _quantile(month_max, 0.9), "p10_min": _quantile(month_min, 0.1),
            "dist_years": f"{dist_years[0]}–{dist_years[-1]}",
        },
        "distribution": {"max": _distribution(month_max), "min": _distribution(month_min)},
        "around_today": {
            "day": target_day, "window_days": WINDOW_DAYS,
            "max": {"median": _quantile(win_max, 0.5), "p10": _quantile(win_max, 0.1), "p90": _quantile(win_max, 0.9),
                    "distribution": _distribution(win_max)},
            "min": {"median": _quantile(win_min, 0.5), "p10": _quantile(win_min, 0.1), "p90": _quantile(win_min, 0.9),
                    "distribution": _distribution(win_min)},
        },
        "records": {"max": record_max, "min": record_min},
        "thresholds": {"max": max_threshold, "min": min_threshold, "min_warm": min_warm},
        "hits": {"max": max_hits, "min": min_hits},
        "insight": insight_text(month_name, max_threshold, min_threshold, max_hits, min_hits, min_warm, years,
                                record_max, record_min),
        "last_official": last_official.isoformat() if last_official else None,
        "source": "Hong Kong Observatory — data iklim harian resmi (CLMMAXT/CLMMINT)",
    }


def default_thresholds(status: Optional[Dict[str, Any]]) -> Tuple[Optional[int], Optional[int]]:
    """Ambang default dari kondisi hari ini: bracket perkiraan max & min (estimasi / prakiraan resmi / favorit pasar)."""
    if not status:
        return None, None

    def fav(market):
        top = max(market or [], key=lambda m: m.get("price_yes") or 0, default=None)
        if not top:
            return None
        import re
        m = re.search(r"(-?\d+)", top.get("bracket") or "")
        return int(m.group(1)) if m else None

    hi = status.get("estimate") or status.get("official_hint")
    lo = status.get("min_estimate") or status.get("min_hint")
    return (bracket_of(hi) if hi is not None else fav(status.get("market")),
            bracket_of(lo) if lo is not None else fav(status.get("min_market")))


def live_summary() -> Dict[str, Any]:
    """Kondisi HK hari ini untuk page: bacaan HKO, perkiraan max/min, prakiraan resmi, harga bracket market."""
    from app.paper_trading.hko_alerts import hko_status

    try:
        s = hko_status()
    except Exception as err:
        logger.warning("Gagal mengambil status HKO: %s", err)
        s = None
    if not s:
        return {"available": False}

    def market(rows):
        return [{"bracket": m.get("bracket"), "price": m.get("price_yes"), "ask": m.get("ask"), "bid": m.get("bid")}
                for m in rows or []]

    official = s.get("official") or {}
    return {
        "available": True, "observed_at": s["observed_at"].isoformat(), "temp": s["temp"],
        "max": s["max"], "min": s["min"], "rate": s.get("rate"),
        "estimate": s.get("estimate"), "min_estimate": s.get("min_estimate"),
        "official_max": official.get("max_hint"), "official_min": official.get("min_hint"),
        "official_text": official.get("text") or official.get("forecast"),
        "final_ok": s.get("final_ok"), "market": market(s.get("market")), "min_market": market(s.get("min_market")),
        "_status": s,
    }


def format_climate_message(month: Optional[int] = None) -> str:
    """/hk iklim [bulan]: insight siap posting + ringkasan klimatologi bulan itu."""
    status = live_summary().get("_status")
    hi, lo = default_thresholds(status)
    r = month_report(month=month, max_threshold=hi, min_threshold=lo)
    s, rec, a = r["summary"], r["records"], r["around_today"]
    lines = [f"🇭🇰 *Klimatologi {r['month_name']} · HKO*", "", r["insight"], "",
             f"Rata-rata {s['dist_years']}: max {s['avg_max']}°C · min {s['avg_min']}°C "
             f"(10% hari max ≥ {s['p90_max']}°C, 10% hari min ≤ {s['p10_min']}°C)",
             f"Sekitar tgl {a['day']} (±{a['window_days']} hari): max median {a['max']['median']}°C "
             f"({a['max']['p10']}–{a['max']['p90']}) · min median {a['min']['median']}°C ({a['min']['p10']}–{a['min']['p90']})"]
    if rec["max"] and rec["min"]:
        lines.append(f"Rekor sejak 1884: max {rec['max']['value']}°C ({rec['max']['date']}) · "
                     f"min {rec['min']['value']}°C ({rec['min']['date']})")
    lines.append("")
    lines.append("`tahun  max   rata²max  min   rata²min`")
    for row in reversed(r["rows"]):
        lines.append(f"`{row['year']}  {row['max']:4.1f}   {row['avg_max']:4.1f}    {row['min']:4.1f}   {row['avg_min']:4.1f}`")
    lines.append("")
    lines.append("Detail & sebaran bracket: dashboard → 🇭🇰 HK Market (/hk)")
    return "\n".join(lines)
