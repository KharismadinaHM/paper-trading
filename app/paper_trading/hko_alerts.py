"""
Alert lonjakan suhu Hong Kong dari data real-time Hong Kong Observatory (per 10 menit, 0.1°C) —
stasiun yang sama dengan sumber resolusi market suhu Hong Kong.

Setiap siklus collector:
1. Bacaan terbaru HKO (suhu + max/min sejak tengah malam) disimpan ke station_readings.
2. Alert Telegram dikirim bila (pada jam HKO_ALERT_HOURS):
   - lonjakan: suhu naik ≥ HKO_ALERT_SPIKE_DEGREES dalam HKO_ALERT_WINDOW_MINUTES terakhir
     (jeda antar alert HKO_ALERT_COOLDOWN_MINUTES), atau
   - derajat baru: max hari ini menembus derajat bulat baru (mis. 32.9 → 33.0°C = pindah bracket).
3. Isi alert: perkiraan max hari ini dari prakiraan Open-Meteo yang dikoreksi bacaan HKO dan dari
   proyeksi laju kenaikan sampai akhir jam puncak, plus harga ask bracket market hari itu.
"""
import csv
import io
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.models import StationAlert, StationReading

logger = get_logger("hko_alerts")

STATION = "HKO"
CITY = "Hong Kong"
HKT = ZoneInfo("Asia/Hong_Kong")
TEMP_URL = "https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather/latest_1min_temperature.csv"
MAXMIN_URL = "https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather/latest_since_midnight_maxmin.csv"
SOURCE_URL = "https://www.hko.gov.hk/en/wxinfo/currwx/current.htm"
FLW_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=flw&lang=en"
WARNSUM_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=warnsum&lang=en"
# HKO: "very hot" ≈ suhu HK Observatory 33°C atau lebih (ambang Very Hot Weather Warning)
VERY_HOT_C = 33.0
MAX_TEMP_RE = re.compile(r"(?:maximum|highest) temperature[^.]*?(\d{2})\s*degrees", re.I)
MIN_TEMP_RE = re.compile(r"(?:minimum|lowest) temperature[^.]*?(\d{2})\s*degrees", re.I)
STATION_NAMES = ("hk observatory", "hong kong observatory")
BRACKET_RE = re.compile(r"(-?\d+)\s*°\s*C(?:\s+or\s+(below|lower|higher|above))?", re.I)


# --- Data HKO ------------------------------------------------------------------------------

def _station_row(text: str) -> Optional[List[str]]:
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 3 and row[1].strip().lower() in STATION_NAMES:
            return row
    return None


def fetch_hko_reading() -> Optional[Dict[str, Any]]:
    """{observed_at, temp, max, min} bacaan terbaru stasiun HK Observatory; None jika gagal."""
    from app.paper_trading.live_market_data import _http

    try:
        temp_row = _station_row(_http(TEMP_URL))
        maxmin_row = _station_row(_http(MAXMIN_URL))
    except Exception as err:
        logger.warning("Gagal mengambil data real-time HKO: %s", err)
        return None
    if not temp_row:
        return None
    reading = {
        "observed_at": datetime.strptime(temp_row[0], "%Y%m%d%H%M").replace(tzinfo=HKT),
        "temp": float(temp_row[2]), "max": None, "min": None,
    }
    if maxmin_row and len(maxmin_row) >= 4 and maxmin_row[0] == temp_row[0]:
        reading.update(max=float(maxmin_row[2]), min=float(maxmin_row[3]))
    return reading


def _hint_for_today(pattern, text: str) -> Optional[float]:
    """Angka suhu dari kalimat prakiraan yang tidak menyebut 'tomorrow' (prakiraan untuk besok diabaikan)."""
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        match = pattern.search(sentence)
        if match and "tomorrow" not in sentence.lower():
            return float(match.group(1))
    return None


def hko_official_forecast() -> Optional[Dict[str, Any]]:
    """
    Prakiraan lokal resmi HKO (siang ini/nanti malam) + status Very Hot Weather Warning:
    {text, period, max_hint, very_hot_warning}. max_hint = angka "maximum temperature ... NN degrees"
    bila disebut, atau 33°C bila siang ini diprakirakan "very hot".
    """
    import json

    from app.paper_trading.live_market_data import _cached, _http

    def load():
        flw = json.loads(_http(FLW_URL))
        try:
            warnings = json.loads(_http(WARNSUM_URL) or "{}")
        except Exception:
            warnings = {}
        text = str(flw.get("forecastDesc") or "").strip()
        period = str(flw.get("forecastPeriod") or "")
        max_hint = None
        today_period = any(w in period.lower() for w in ("this afternoon", "today", "this morning"))
        match = _hint_for_today(MAX_TEMP_RE, text)
        if match is not None and today_period:
            max_hint = match
        elif today_period and re.search(r"very hot", text, re.I):
            max_hint = VERY_HOT_C
        min_hint = _hint_for_today(MIN_TEMP_RE, text)  # "tonight"/"this morning" relevan; "tomorrow" tidak
        return {"text": text, "period": period, "max_hint": max_hint, "min_hint": min_hint,
                "very_hot_warning": isinstance(warnings, dict) and "WHOT" in warnings}

    try:
        return _cached("hko_flw", 10 * 60, load)
    except Exception as err:
        logger.warning("Gagal mengambil prakiraan resmi HKO: %s", err)
        return None


def record_reading(reading: Dict[str, Any], db) -> bool:
    """Simpan bacaan jika belum ada (kunci station + observed_at). True jika baris baru."""
    ts = reading["observed_at"].astimezone(timezone.utc)
    if db.get(StationReading, (STATION, ts)) is not None:
        return False
    db.add(StationReading(
        station=STATION, observed_at=ts, temp=Decimal(str(reading["temp"])),
        max_since_midnight=Decimal(str(reading["max"])) if reading.get("max") is not None else None,
        min_since_midnight=Decimal(str(reading["min"])) if reading.get("min") is not None else None,
    ))
    db.flush()
    return True


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def readings_today(db, now: datetime) -> List[StationReading]:
    start = datetime.combine(now.astimezone(HKT).date(), datetime.min.time(), tzinfo=HKT).astimezone(timezone.utc)
    return (db.query(StationReading)
            .filter(StationReading.station == STATION, StationReading.observed_at >= start)
            .order_by(StationReading.observed_at).all())


# --- Analisis -----------------------------------------------------------------------------

def _parse_bracket(label: str):
    m = BRACKET_RE.search(label or "")
    if not m:
        return None
    lo = hi = int(m.group(1))
    tail = (m.group(2) or "").lower()
    if tail in ("below", "lower"):
        lo = -math.inf
    elif tail in ("higher", "above"):
        hi = math.inf
    return lo, hi


def bracket_for(value: float, labels: List[str]) -> Optional[str]:
    """Bracket market yang memuat nilai HKO 0.1°C (32.8 → '32°C', diasumsikan 32.0–32.9)."""
    whole = math.floor(value + 1e-9)
    for label in labels:
        b = _parse_bracket(label)
        if b and b[0] <= whole <= b[1]:
            return label
    return None


def _today_market(now: datetime, kind: str = "highest", day: Optional["date"] = None) -> List[Dict[str, Any]]:
    """Bracket market '<kind> temperature in Hong Kong' hari ini (atau `day`) + harga ask order book."""
    from app.paper_service import get_market_snapshots
    from app.paper_trading.cities import resolve_city
    from app.paper_trading.live_market_data import fetch_order_books
    from app.paper_trading.weather_peaks import _as_datetime, bracket_label, parse_temperature_market

    today = day or now.astimezone(HKT).date()
    markets = []
    for m in get_market_snapshots(now=now, include_resolved=False):
        parsed = parse_temperature_market(m["market_name"], _as_datetime(m.get("end_date")), now)
        if parsed and parsed.kind == kind and parsed.local_date == today and resolve_city(parsed.city) == CITY:
            markets.append({"bracket": bracket_label(m["market_name"]), "yes_token_id": m.get("yes_token_id"),
                            "market_id": m.get("market_id"), "market_name": m.get("market_name"),
                            "price_yes": float(m["price_yes"]) if m.get("price_yes") is not None else None})
    books = fetch_order_books(m["yes_token_id"] for m in markets)
    for m in markets:
        book = books.get(str(m["yes_token_id"])) or {}
        m.update(ask=book.get("ask"), bid=book.get("bid"))
    markets.sort(key=lambda m: -(m["price_yes"] or 0))
    return markets


def can_be_final(now: datetime, temp: Optional[float], observed_max: Optional[float]) -> bool:
    """
    Max HK boleh dianggap final hanya setelah HKO_FINAL_HOUR (HKT) atau bila suhu sudah turun
    ≥ HKO_FINAL_DROP dari max. Jam puncak khas (14–15) saja tidak cukup: suhu bisa naik lagi sore hari.
    """
    if now.astimezone(HKT).hour >= settings.HKO_FINAL_HOUR:
        return True
    return temp is not None and observed_max is not None and temp <= observed_max - settings.HKO_FINAL_DROP


def hko_status(now: Optional[datetime] = None, db=None) -> Optional[Dict[str, Any]]:
    """
    Status HK terkini: bacaan, laju °/jam (60 menit), kenaikan dalam jendela alert, perkiraan max
    (prakiraan terkoreksi & proyeksi tren), estimasi gabungan, dan bracket market hari ini.
    """
    from app.paper_trading import weather_outlook as wo
    from app.paper_trading.weather_peaks import recommendation_window

    now = now or datetime.now(timezone.utc)
    close = db is None
    db = db or get_db_session()
    try:
        reading = fetch_hko_reading()
        if reading:
            try:
                record_reading(reading, db)
                db.commit()
            except IntegrityError:
                # Proses lain (collector, atau request dashboard paralel) baru saja menyimpan bacaan yang sama
                db.rollback()
        rows = readings_today(db, now)
    finally:
        if close:
            db.close()
    if not rows:
        return None
    latest = rows[-1]
    latest_at = _aware(latest.observed_at)
    temp = float(latest.temp)
    series = [(_aware(r.observed_at), float(r.temp)) for r in rows]
    observed_max = max([float(r.max_since_midnight) for r in rows if r.max_since_midnight is not None]
                       + [t for _, t in series])
    window_start = latest_at - timedelta(minutes=settings.HKO_ALERT_WINDOW_MINUTES)
    window = [t for ts, t in series if ts >= window_start]
    rise = round(temp - min(window), 1) if window else 0.0
    rate = wo.temperature_trend(series, now=latest_at, hours=1.0, min_span_minutes=20)  # HKO per 10 menit

    local_date = now.astimezone(HKT).date()
    peak = recommendation_window(CITY, "highest", local_date)
    final_ok = can_be_final(now, temp, observed_max)
    peak_passed = peak is not None and now >= peak.peak_end and (rate is None or rate <= 0.1) and final_ok
    forecast = wo.fetch_hourly_forecast(*wo.HKO_COORDS)
    out = wo.outlook("highest", HKT, local_date, observed_max, None, temp, latest_at, forecast, "C", now,
                     peak_passed_hint=peak_passed)

    projection = None
    if rate and rate > 0 and peak is not None:
        # Sebelum puncak: sampai akhir jam puncak. Sesudahnya (kenaikan sore): sampai batas final, maks 2 jam.
        final_at = datetime.combine(local_date, datetime.min.time(), tzinfo=HKT) + timedelta(hours=settings.HKO_FINAL_HOUR)
        until = peak.peak_end if now < peak.peak_end else min(final_at, now + timedelta(hours=2))
        if until > now:
            hours = min((until - now).total_seconds() / 3600, 4.0)
            projection = {"value": round(temp + rate * hours, 1), "until": until.astimezone(HKT), "rate": rate}

    official = hko_official_forecast()
    official_hint = (official or {}).get("max_hint") if not (out and out.get("reason") == "peak") else None
    # Untuk HK, prakiraan resmi HKO lebih diutamakan daripada Open-Meteo
    model_value = official_hint if official_hint is not None else (
        (out or {}).get("value") if out and not out["passed"] else None)
    candidates = [c for c in (model_value, (projection or {}).get("value")) if c is not None]
    peak_passed = bool(out and out["passed"] and out.get("reason") in ("peak", "day_end")) and final_ok
    # sisa hari tak melampaui max tercatat — untuk HK hanya bila aturan final terpenuhi
    settled = bool(out and out["passed"]) and official_hint is None and final_ok
    if candidates:
        estimate = round(max(observed_max, sum(candidates) / len(candidates)), 1)
    else:
        estimate = observed_max if settled else None  # tanpa prakiraan & tren: belum bisa diperkirakan
    try:
        market = _today_market(now)
    except Exception as err:
        logger.warning("Gagal mengambil market Hong Kong hari ini: %s", err)
        market = []

    # Suhu terendah hari kalender: bisa masih turun sampai tengah malam
    observed_min = min([float(r.min_since_midnight) for r in rows if r.min_since_midnight is not None]
                       + [t for _, t in series])
    tied = [ts for ts, t in series if abs(t - observed_min) < 1e-9]
    min_at = (tied[0] + (tied[-1] - tied[0]) / 2).astimezone(HKT) if tied else None
    min_out = wo.outlook("lowest", HKT, local_date, observed_min, min_at, temp, latest_at, forecast, "C", now)
    min_hint = (official or {}).get("min_hint")
    if min_hint is not None and min_hint < observed_min:
        min_estimate = min_hint
    elif min_out and not min_out["passed"]:
        min_estimate = min_out["value"]
    else:
        min_estimate = observed_min if min_out else None
    try:
        min_market = _today_market(now, "lowest")
    except Exception as err:
        logger.warning("Gagal mengambil market suhu terendah Hong Kong: %s", err)
        min_market = []
    return {
        "observed_at": latest_at.astimezone(HKT), "temp": temp, "max": observed_max,
        "min": float(latest.min_since_midnight) if latest.min_since_midnight is not None else min(t for _, t in series),
        "rise": rise, "rate": rate, "outlook": out, "projection": projection, "estimate": estimate,
        "official": official, "official_hint": official_hint,
        "peak": peak, "peak_passed": peak_passed, "final_ok": final_ok, "market": market,
        "min_at": min_at, "min_outlook": min_out, "min_estimate": min_estimate, "min_hint": min_hint,
        "min_market": min_market,
        "previous_max": max([float(r.max_since_midnight) for r in rows[:-1] if r.max_since_midnight is not None]
                            + [float(r.temp) for r in rows[:-1]], default=None),
    }


# --- Pesan & pengiriman -------------------------------------------------------------------

def _odd(price: Optional[float]) -> str:
    from app.paper_trading.recommendation_alerts import _format_odd
    return _format_odd(price)


def format_hko_message(status: Dict[str, Any], reasons: List[str]) -> str:
    wib = ZoneInfo(settings.NOTIFY_TIMEZONE)
    label = settings.NOTIFY_TIMEZONE_LABEL
    at = status["observed_at"]
    rate = f" · laju {status['rate']:+.1f}°/jam" if status.get("rate") is not None else ""
    lines = reasons + [
        f"Sekarang {status['temp']:.1f}°C (HKO {at:%H:%M} HKT / {at.astimezone(wib):%H:%M} {label})"
        f" · max hari ini {status['max']:.1f}°C{rate}",
    ]
    labels = [m["bracket"] for m in status["market"]]
    est_bracket = bracket_for(status["estimate"], labels) if labels and status["estimate"] is not None else None
    if status["estimate"] is None:
        lines.append("🧭 Perkiraan max: belum bisa dihitung (prakiraan tidak tersedia & data tren belum cukup)")
    else:
        lines.append(f"🧭 Perkiraan max hari ini ±{status['estimate']:.1f}°C"
                     + (f" (bracket ≈ {est_bracket})" if est_bracket else ""))
    out, proj = status.get("outlook"), status.get("projection")
    official = status.get("official") or {}
    if official.get("text"):
        hint = f" (≈{status['official_hint']:.0f}°C)" if status.get("official_hint") is not None else ""
        lines.append(f"   • Prakiraan resmi HKO: \"{official['text']}\"{hint}")
    if official.get("very_hot_warning"):
        lines.append("   • ⚠️ Very Hot Weather Warning sedang berlaku")
    if status["peak_passed"]:
        lines.append(f"   • Puncak kemungkinan sudah lewat — max kemungkinan tetap {status['max']:.1f}°C")
    elif out and not out["passed"] and out.get("at"):
        lines.append(f"   • Prakiraan Open-Meteo + koreksi HKO: {out['value']:.1f}°C sekitar {out['at']:%H:%M}")
    elif out and out.get("reason") == "forecast" and out.get("next_at"):
        lines.append(f"   • Prakiraan Open-Meteo + koreksi HKO: ±{out['next_value']:.1f}°C sekitar "
                     f"{out['next_at']:%H:%M}, tidak melebihi max tercatat")
    if proj:
        lines.append(f"   • Proyeksi tren ({proj['rate']:+.1f}°/jam s/d {proj['until']:%H:%M}): {proj['value']:.1f}°C")
    if status["market"]:
        top = status["market"][:4]
        lines.append("Market: " + " · ".join(
            f"{'👉 ' if m['bracket'] == est_bracket else ''}{m['bracket']} "
            + (f"ask {_odd(m['ask'])}" if m.get("ask") is not None else f"mid {_odd(m.get('price_yes'))}")
            for m in top))
    lines += _min_lines(status)
    lines.append(SOURCE_URL)
    lines.append("Perkiraan, bukan kepastian. Paper trading, bukan saran finansial.")
    return "\n".join(lines)


def _min_lines(status: Dict[str, Any]) -> List[str]:
    """Bagian suhu terendah hari ini: tercatat, perkiraan sisa hari, prakiraan resmi, market."""
    if status.get("min") is None:
        return []
    at = f" (≈{status['min_at']:%H:%M})" if status.get("min_at") else ""
    lines = [f"❄️ Min hari ini tercatat {status['min']:.1f}°C{at}"]
    out = status.get("min_outlook")
    labels = [m["bracket"] for m in status.get("min_market") or []]
    estimate = status.get("min_estimate")
    est_bracket = bracket_for(estimate, labels) if labels and estimate is not None else None
    if estimate is None:
        lines.append("🧭 Perkiraan min: belum bisa dihitung (prakiraan tidak tersedia)")
    else:
        lines.append(f"🧭 Perkiraan min hari ini ±{estimate:.1f}°C" + (f" (bracket ≈ {est_bracket})" if est_bracket else ""))
    if status.get("min_hint") is not None:
        lines.append(f"   • Prakiraan resmi HKO: minimum ±{status['min_hint']:.0f}°C")
    if out and not out["passed"] and out.get("at"):
        lines.append(f"   • Prakiraan Open-Meteo + koreksi HKO: {out['value']:.1f}°C sekitar {out['at']:%H:%M} "
                     "(min hari kalender bisa turun sampai tengah malam)")
    elif out and out["passed"]:
        nxt = (f" — prakiraan berikutnya ±{out['next_value']:.1f}°C sekitar {out['next_at']:%H:%M}"
               if out.get("next_at") else "")
        lines.append(f"   • Sisa hari diperkirakan tidak lebih dingin: min kemungkinan tetap {status['min']:.1f}°C{nxt}")
    if status.get("min_market"):
        lines.append("Market min: " + " · ".join(
            f"{'👉 ' if m['bracket'] == est_bracket else ''}{m['bracket']} "
            + (f"ask {_odd(m['ask'])}" if m.get("ask") is not None else f"mid {_odd(m.get('price_yes'))}")
            for m in status["min_market"][:4]))
    return lines


def _market_price(status: Dict[str, Any], label: Optional[str]) -> Optional[str]:
    m = next((m for m in status.get("market") or [] if m["bracket"] == label), None)
    if not m:
        return None
    return f"ask {_odd(m['ask'])}" if m.get("ask") is not None else f"mid {_odd(m.get('price_yes'))}"


def held_hk_positions(now: datetime) -> List[Dict[str, Any]]:
    """
    Posisi YES di market 'highest temperature in Hong Kong' hari ini, dari wallet Polymarket sendiri
    (POLYMARKET_WALLET_ADDRESS, read-only) dan akun paper. [{bracket, shares, source}]
    """
    from app.paper_trading.cities import resolve_city
    from app.paper_trading.weather_peaks import bracket_label, parse_temperature_market

    today = now.astimezone(HKT).date()

    def is_hk_today(title: str) -> bool:
        parsed = parse_temperature_market(title or "", now=now)
        return bool(parsed and parsed.kind == "highest" and parsed.local_date == today
                    and resolve_city(parsed.city) == CITY)

    held: List[Dict[str, Any]] = []
    try:
        from app.paper_trading.my_wallet import wallet_address
        from app.paper_trading.wallets import _get
        address = wallet_address()
        if address:
            for p in _get("/positions", user=address, limit=500, sizeThreshold=0.01) or []:
                if str(p.get("outcome") or "").lower() == "yes" and is_hk_today(p.get("title")):
                    held.append({"bracket": bracket_label(p["title"]), "shares": float(p.get("size") or 0),
                                 "source": "wallet"})
    except Exception as err:
        logger.warning("Gagal membaca posisi wallet untuk alert HK: %s", err)
    try:
        from app.paper_service import get_open_positions
        for p in get_open_positions():
            if str(p.get("side")).upper() == "YES" and is_hk_today(p.get("market_name")):
                held.append({"bracket": bracket_label(p["market_name"]), "shares": float(p.get("shares") or 0),
                             "source": "paper"})
    except Exception as err:
        logger.warning("Gagal membaca posisi paper untuk alert HK: %s", err)
    return held


def near_next_degree(status: Dict[str, Any], now: datetime) -> Optional[int]:
    """
    Derajat berikutnya bila max hari ini sudah ≥ X + HKO_NEAR_DEGREE_FRACTION, masih sebelum batas final,
    dan suhu masih naik atau bertahan dekat max (≤ 0.3°C di bawahnya). Contoh: max 33.8 → 34.
    """
    observed = status["max"]
    fraction = observed - math.floor(observed + 1e-9)
    if fraction + 1e-9 < settings.HKO_NEAR_DEGREE_FRACTION or status.get("final_ok"):
        return None
    rising = (status.get("rate") or 0) > 0 or status["temp"] >= observed - 0.3
    return math.floor(observed + 1e-9) + 1 if rising else None


def _in_alert_hours(now: datetime) -> bool:
    try:
        start, end = (int(x) for x in str(settings.HKO_ALERT_HOURS).split("-", 1))
    except ValueError:
        start, end = 7, 19
    return start <= now.astimezone(HKT).hour <= end


def check_hko_alerts(now: Optional[datetime] = None) -> Optional[str]:
    """Satu siklus: simpan bacaan HKO, kirim alert bila ada lonjakan / derajat baru. Kembalikan teks terkirim."""
    if not settings.HKO_ALERTS:
        return None
    from app.paper_trading.telegram import send_telegram_message

    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    try:
        status = hko_status(now=now, db=db)
        if status is None or not _in_alert_hours(now):
            return None
        if now - status["observed_at"] > timedelta(minutes=20):  # bacaan lama → jangan alert ulang
            return None
        local_date = now.astimezone(HKT).date().isoformat()
        sent_today = db.query(StationAlert).filter_by(station=STATION, local_date=local_date).all()
        reasons, records = [], []

        last_spike = max((_aware(a.sent_at) for a in sent_today if a.kind == "spike"), default=None)
        cooldown = timedelta(minutes=settings.HKO_ALERT_COOLDOWN_MINUTES)
        if status["rise"] >= settings.HKO_ALERT_SPIKE_DEGREES and (last_spike is None or now - last_spike >= cooldown):
            reasons.append(f"🚨 #HongKong suhu melonjak +{status['rise']:.1f}°C dalam "
                           f"{settings.HKO_ALERT_WINDOW_MINUTES} menit")
            records.append(("spike", status["rise"]))

        degree = math.floor(status["max"] + 1e-9)
        prev = status.get("previous_max")
        alerted_degrees = {int(a.value) for a in sent_today if a.kind == "degree" and a.value is not None}
        if prev is not None and degree > math.floor(prev + 1e-9) and degree not in alerted_degrees:
            reasons.append(f"🔺 #HongKong max hari ini menembus {degree}°C ({prev:.1f} → {status['max']:.1f}°C)")
            records.append(("degree", degree))

        target = near_next_degree(status, now)
        alerted_near = {int(a.value) for a in sent_today if a.kind == "near" and a.value is not None}
        if target is not None and target not in alerted_near:
            labels = [m["bracket"] for m in status["market"]]
            next_label = bracket_for(float(target), labels) if labels else f"{target}°C"
            price = _market_price(status, next_label)
            reasons.append(f"⚠️ #HongKong max {status['max']:.1f}°C — tinggal {target - status['max']:.1f}°C ke {target}°C"
                           + (f" (bracket {next_label} {price})" if price else "")
                           + f". Belum final sebelum {settings.HKO_FINAL_HOUR}:00 HKT.")
            records.append(("near", target))

        # Risiko posisi: bracket YES yang dipegang kalah bila max menembus batas atasnya
        alerted_pos = {int(a.value) for a in sent_today if a.kind == "position" and a.value is not None}
        for pos in held_hk_positions(now):
            b = _parse_bracket(pos["bracket"])
            if not b or b[1] == math.inf:
                continue
            ceiling = b[1] + 1  # bracket 33°C kalah bila max ≥ 34.0
            if (ceiling - status["max"] <= 1 - settings.HKO_NEAR_DEGREE_FRACTION + 1e-9 and status["max"] < ceiling
                    and not status.get("final_ok") and int(ceiling) not in alerted_pos):
                reasons.append(f"🛑 Posisi {pos['source']} Anda: {pos['bracket']} YES ({pos['shares']:,.1f} shares) berisiko — "
                               f"max {status['max']:.1f}°C, tinggal {ceiling - status['max']:.1f}°C ke {int(ceiling)}°C"
                               + (f" · {pos['bracket']} {_market_price(status, pos['bracket'])}"
                                  if _market_price(status, pos["bracket"]) else ""))
                records.append(("position", int(ceiling)))
                alerted_pos.add(int(ceiling))

        if not reasons:
            return None
        text = format_hko_message(status, reasons)
        result = send_telegram_message(text)
        if not result.get("success"):
            logger.warning("Alert HKO tidak terkirim: %s", result.get("error"))
            return None
        for kind, value in records:
            db.add(StationAlert(station=STATION, kind=kind, local_date=local_date,
                                value=Decimal(str(value)), sent_at=now))
        db.commit()
        logger.info("Alert HKO terkirim: %s", [k for k, _ in records])
        return text
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def run_hko_alerts() -> bool:
    """Dipanggil dari loop collector; tidak pernah melempar exception."""
    try:
        return check_hko_alerts() is not None
    except Exception as err:
        logger.error("Gagal memproses alert HKO: %s", err, exc_info=True)
        return False


def readings_for_day(day: Optional["date"] = None) -> List[Dict[str, Any]]:
    """Bacaan HKO tersimpan untuk satu hari HKT (default hari ini), urut waktu."""
    from datetime import date as _date

    day = day or datetime.now(HKT).date()
    start = datetime.combine(day, datetime.min.time(), tzinfo=HKT).astimezone(timezone.utc)
    db = get_db_session()
    try:
        rows = (db.query(StationReading).filter(StationReading.station == STATION,
                                                StationReading.observed_at >= start,
                                                StationReading.observed_at < start + timedelta(days=1))
                .order_by(StationReading.observed_at).all())
        alerts = db.query(StationAlert).filter_by(station=STATION, local_date=day.isoformat()).all()
    finally:
        db.close()
    alert_times = [(_aware(a.sent_at).astimezone(HKT), a.kind, a.value) for a in alerts]
    out = []
    for r in rows:
        at = _aware(r.observed_at).astimezone(HKT)
        out.append({"at": at, "temp": float(r.temp),
                    "max": float(r.max_since_midnight) if r.max_since_midnight is not None else None,
                    "min": float(r.min_since_midnight) if r.min_since_midnight is not None else None,
                    "alerts": [f"{k}:{float(v):g}" for t, k, v in alert_times
                               if at <= t < at + timedelta(minutes=10)]})
    return out


def readings_between(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """Bacaan HKO tersimpan dalam rentang waktu, urut waktu."""
    db = get_db_session()
    try:
        rows = (db.query(StationReading).filter(StationReading.station == STATION,
                                                StationReading.observed_at >= start.astimezone(timezone.utc),
                                                StationReading.observed_at <= end.astimezone(timezone.utc))
                .order_by(StationReading.observed_at).all())
        alerts = (db.query(StationAlert).filter(StationAlert.station == STATION,
                                                StationAlert.sent_at >= start.astimezone(timezone.utc)).all())
    finally:
        db.close()
    alert_times = [_aware(a.sent_at).astimezone(HKT) for a in alerts]
    out = []
    for r in rows:
        at = _aware(r.observed_at).astimezone(HKT)
        out.append({"at": at, "temp": float(r.temp),
                    "max": float(r.max_since_midnight) if r.max_since_midnight is not None else None,
                    "min": float(r.min_since_midnight) if r.min_since_midnight is not None else None,
                    "alerts": [t for t in alert_times if at <= t < at + timedelta(minutes=10)]})
    return out


def format_history(day: Optional["date"] = None, now: Optional[datetime] = None) -> str:
    """
    /hk riwayat: bacaan HKO 24 jam ke belakang (atau satu tanggal HKT bila `day` diisi). 3 jam terakhir per
    10 menit, sebelumnya per 30 menit agar muat satu pesan Telegram.
    """
    now = now or datetime.now(timezone.utc)
    if day is not None:
        start = datetime.combine(day, datetime.min.time(), tzinfo=HKT)
        end = start + timedelta(days=1) - timedelta(seconds=1)
        title = f"🇭🇰 *Riwayat HKO* {day:%d %b %Y} (HKT)"
    else:
        end = now.astimezone(HKT)
        start = end - timedelta(hours=24)
        title = f"🇭🇰 *Riwayat HKO 24 jam* ({start:%d %b %H:%M} – {end:%d %b %H:%M} HKT)"
    rows = readings_between(start, end)
    if not rows:
        return f"{title}\nBelum ada bacaan HKO tersimpan pada rentang ini."
    detail_from = rows[-1]["at"] - timedelta(hours=3)
    shown = [r for r in rows if r["at"] >= detail_from or r["at"].minute % 30 == 0]
    lines = [title, "`tgl jam    suhu   max    Δ`"]
    prev = None
    for r in shown:
        delta = f"{r['temp'] - prev:+.1f}" if prev is not None else "  "
        prev = r["temp"]
        mark = " 🔺" if r["alerts"] else ""
        max_txt = f"{r['max']:.1f}" if r["max"] is not None else "-"
        lines.append(f"`{r['at']:%d %H:%M}  {r['temp']:4.1f}  {max_txt:>4}  {delta:>4}`{mark}")
    return "\n".join(lines)


def readings_csv(day: Optional["date"] = None) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["observed_at_hkt", "temp_c", "max_since_midnight_c", "min_since_midnight_c", "alerts"])
    for r in readings_for_day(day):
        writer.writerow([r["at"].isoformat(), r["temp"], r["max"], r["min"], ";".join(r["alerts"])])
    return buf.getvalue()


def build_hk_command_message() -> str:
    """Balasan /hk: status Hong Kong terkini tanpa menunggu alert."""
    status = hko_status()
    if status is None:
        return "🌡️ Data HKO belum tersedia. Coba lagi beberapa menit lagi."
    rise = (f"Perubahan {settings.HKO_ALERT_WINDOW_MINUTES} menit terakhir: {status['rise']:+.1f}°C"
            if status.get("rise") is not None else "")
    return format_hko_message(status, ["🇭🇰 *Hong Kong · HKO real-time*"] + ([rise] if rise else []))
