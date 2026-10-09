"""
Prakiraan & kondisi cuaca Hong Kong untuk halaman /hk, AI, dan model hari besok.

- current_weather(): laporan cuaca terkini HKO (rhrread): ikon/kondisi, kelembapan, hujan, UV, peringatan.
- nine_day(): prakiraan 9 hari resmi HKO (fnd): max/min, kondisi, peluang hujan signifikan (PSR).
- hourly_outlook(): per jam s/d 48 jam dari Open-Meteo (suhu, kode cuaca, peluang hujan, kelembapan, angin,
  awan); suhu dikoreksi bias bacaan HKO terkini yang meluruh dengan jarak (sama seperti tabel per jam).
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.core.logging import get_logger
from app.paper_trading.hko_alerts import HKT

logger = get_logger("hk_forecast")

RHRREAD_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=rhrread&lang=en"
FND_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=fnd&lang=en"
HOURLY_VARS = "temperature_2m,weather_code,precipitation_probability,relative_humidity_2m,wind_speed_10m,cloud_cover"

# Ikon HKO → (emoji, keterangan)
HKO_ICONS = {
    50: ("☀️", "Cerah"), 51: ("🌤", "Cerah berawan"), 52: ("⛅", "Cerah sesekali"), 53: ("🌦", "Cerah, sedikit hujan"),
    54: ("🌦", "Cerah sesekali, hujan"), 60: ("☁️", "Berawan"), 61: ("☁️", "Mendung"), 62: ("🌦", "Hujan ringan"),
    63: ("🌧", "Hujan"), 64: ("🌧", "Hujan lebat"), 65: ("⛈", "Badai petir"), 70: ("🌙", "Cerah (malam)"),
    71: ("🌙", "Cerah (malam)"), 72: ("🌙", "Cerah (malam)"), 73: ("🌙", "Cerah (malam)"), 74: ("🌙", "Cerah (malam)"),
    75: ("🌙", "Cerah (malam)"), 76: ("☁️", "Kebanyakan berawan"), 77: ("🌙", "Kebanyakan cerah"), 80: ("💨", "Berangin"),
    81: ("🏜", "Kering"), 82: ("💧", "Lembap"), 83: ("🌫", "Kabut"), 84: ("🌫", "Kabut tipis"), 85: ("🌫", "Berasap"),
    90: ("🔥", "Panas"), 91: ("🌡", "Hangat"), 92: ("🍃", "Sejuk"), 93: ("🥶", "Dingin"),
}

# Kode cuaca WMO (Open-Meteo) → (emoji, keterangan)
WMO = {
    0: ("☀️", "Cerah"), 1: ("🌤", "Kebanyakan cerah"), 2: ("⛅", "Berawan sebagian"), 3: ("☁️", "Mendung"),
    45: ("🌫", "Kabut"), 48: ("🌫", "Kabut beku"), 51: ("🌦", "Gerimis ringan"), 53: ("🌦", "Gerimis"),
    55: ("🌧", "Gerimis lebat"), 61: ("🌦", "Hujan ringan"), 63: ("🌧", "Hujan"), 65: ("🌧", "Hujan lebat"),
    80: ("🌦", "Hujan lokal ringan"), 81: ("🌧", "Hujan lokal"), 82: ("🌧", "Hujan lokal lebat"),
    95: ("⛈", "Badai petir"), 96: ("⛈", "Badai petir + es"), 99: ("⛈", "Badai petir + es"),
}


def _night(hour: int) -> bool:
    return hour < 6 or hour >= 19


def describe_wmo(code: Optional[int], hour: Optional[int] = None) -> Dict[str, str]:
    icon, text = WMO.get(int(code), ("·", "-")) if code is not None else ("·", "-")
    if hour is not None and _night(hour) and code in (0, 1):
        icon = "🌙"
    return {"icon": icon, "text": text}


def describe_hko(icon: Optional[int]) -> Dict[str, str]:
    e, text = HKO_ICONS.get(int(icon), ("·", "-")) if icon is not None else ("·", "-")
    return {"icon": e, "text": text}


def current_weather() -> Optional[Dict[str, Any]]:
    """Laporan cuaca terkini HKO; None bila gagal."""
    from app.paper_trading.live_market_data import _cached, _http

    def load():
        data = json.loads(_http(RHRREAD_URL))
        temp = next((d.get("value") for d in (data.get("temperature") or {}).get("data", [])
                     if d.get("place") == "Hong Kong Observatory"), None)
        humidity = next((d.get("value") for d in (data.get("humidity") or {}).get("data", [])
                         if d.get("place") == "Hong Kong Observatory"), None)
        rain = [d.get("max") for d in (data.get("rainfall") or {}).get("data", []) if isinstance(d.get("max"), (int, float))]
        uv = ((data.get("uvindex") or {}).get("data") or [{}])[0] if isinstance(data.get("uvindex"), dict) else {}
        icons = data.get("icon") or []
        warnings = data.get("warningMessage") or []
        return {"updated": data.get("updateTime"), "temp": temp, "humidity": humidity,
                "rain_max_mm": max(rain) if rain else 0, "uv": uv.get("value"), "uv_desc": uv.get("desc"),
                "icon_code": icons[0] if icons else None, **describe_hko(icons[0] if icons else None),
                "warnings": [w for w in warnings if isinstance(w, str)][:3]}

    try:
        return _cached("hko_rhrread", 5 * 60, load)
    except Exception as err:
        logger.warning("Gagal mengambil cuaca terkini HKO: %s", err)
        return None


def nine_day() -> Dict[str, Any]:
    """Prakiraan 9 hari HKO: {situation, updated, days: [{date, week, max, min, rh, weather, icon, psr}]}."""
    from app.paper_trading.live_market_data import _cached, _http

    def load():
        data = json.loads(_http(FND_URL))
        days = []
        for d in data.get("weatherForecast") or []:
            try:
                day = datetime.strptime(str(d.get("forecastDate")), "%Y%m%d").date()
            except ValueError:
                continue
            days.append({
                "date": day.isoformat(), "week": d.get("week"),
                "max": (d.get("forecastMaxtemp") or {}).get("value"), "min": (d.get("forecastMintemp") or {}).get("value"),
                "rh_max": (d.get("forecastMaxrh") or {}).get("value"), "rh_min": (d.get("forecastMinrh") or {}).get("value"),
                "weather": d.get("forecastWeather"), "wind": d.get("forecastWind"), "psr": d.get("PSR"),
                "icon_code": d.get("ForecastIcon"), **describe_hko(d.get("ForecastIcon")),
            })
        return {"situation": data.get("generalSituation"), "updated": data.get("updateTime"), "days": days}

    try:
        return _cached("hko_fnd", 30 * 60, load)
    except Exception as err:
        logger.warning("Gagal mengambil prakiraan 9 hari HKO: %s", err)
        return {"situation": None, "updated": None, "days": []}


def _open_meteo_hourly() -> List[Dict[str, Any]]:
    from app.paper_trading.live_market_data import _cached, _http
    from app.paper_trading.weather_outlook import FORECAST_TTL, FORECAST_URL, HKO_COORDS

    lat, lon = HKO_COORDS

    def load():
        url = (f"{FORECAST_URL}?latitude={lat:.4f}&longitude={lon:.4f}&hourly={HOURLY_VARS}"
               "&timezone=GMT&forecast_days=3")
        h = json.loads(_http(url)).get("hourly") or {}
        out = []
        for i, t in enumerate(h.get("time", [])):
            row = {"at": datetime.fromisoformat(t).replace(tzinfo=timezone.utc)}
            for var in HOURLY_VARS.split(","):
                values = h.get(var) or []
                row[var] = values[i] if i < len(values) else None
            out.append(row)
        return out

    try:
        return _cached("hk_hourly_outlook", FORECAST_TTL, load)
    except Exception as err:
        logger.warning("Gagal mengambil prakiraan per jam Open-Meteo HK: %s", err)
        return []


def hourly_outlook(now: Optional[datetime] = None, hours: int = 24) -> List[Dict[str, Any]]:
    """Per jam mulai jam berikutnya: suhu (model + bias HKO meluruh), kondisi, peluang hujan, RH, angin, awan."""
    from app.core.database import get_db_session
    from app.paper_trading.hko_hourly import _readings, bias_weight

    now = now or datetime.now(timezone.utc)
    rows = _open_meteo_hourly()
    if not rows:
        return []
    db = get_db_session()
    try:
        readings = _readings(db, now - timedelta(hours=3), now + timedelta(minutes=1))
    finally:
        db.close()
    offset, latest_at = 0.0, now
    if readings:
        latest_at, latest = readings[-1]
        base = latest_at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        by_time = {r["at"]: r["temperature_2m"] for r in rows}
        v0, v1 = by_time.get(base), by_time.get(base + timedelta(hours=1))
        if v0 is not None:
            frac = (latest_at - base).total_seconds() / 3600
            model_now = v0 + ((v1 - v0) * frac if v1 is not None else 0)
            offset = latest - model_now
    first = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    out = []
    for r in rows:
        if r["at"] < first or len(out) >= hours or r["temperature_2m"] is None:
            continue
        lead = (r["at"] - latest_at).total_seconds() / 3600
        local = r["at"].astimezone(HKT)
        out.append({
            "at": local.isoformat(), "hour": local.strftime("%H:%M"), "date": local.date().isoformat(),
            "temp": round(r["temperature_2m"] + offset * bias_weight(lead), 1), "model_temp": r["temperature_2m"],
            "rain_prob": r.get("precipitation_probability"), "humidity": r.get("relative_humidity_2m"),
            "wind_kmh": r.get("wind_speed_10m"), "cloud": r.get("cloud_cover"),
            **describe_wmo(r.get("weather_code"), local.hour),
        })
    return out


def forecast_payload(now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    return {"now": current_weather(), "hours": hourly_outlook(now, hours=24), "nine_day": nine_day()}
