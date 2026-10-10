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


def ensemble_table(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Per model: max & min sisa hari ini dan besok (dikoreksi bacaan HKO terakhir)."""
    from app.core.database import get_db_session
    from app.paper_trading.hko_hourly import _readings

    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    try:
        readings = _readings(db, now - timedelta(hours=3), now + timedelta(minutes=1))
    finally:
        db.close()
    latest_at, latest = readings[-1] if readings else (None, None)
    local = now.astimezone(HKT)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=HKT)
    parts = {"today_max": ("highest", now, midnight), "today_min": ("lowest", now, midnight),
             "tomorrow_max": ("highest", midnight, midnight + timedelta(days=1)),
             "tomorrow_min": ("lowest", midnight, midnight + timedelta(days=1))}
    table: Dict[str, Dict[str, Any]] = {}
    for key, (kind, start, end) in parts.items():
        for model, v in ensemble_extremes(kind, start, end, latest_at, latest).items():
            table.setdefault(model, {"model": model, "name": MODEL_NAMES.get(model, model)})[key] = v["value"]
    return [table[m] for m in ENSEMBLE_MODELS if m in table]


def forecast_payload(now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    rain = rain_signal(now)
    return {"now": current_weather(), "hours": hourly_outlook(now, hours=24), "nine_day": nine_day(),
            "nowcast": rain.get("nowcast"), "warnings": rain.get("warnings"), "rain": {"expected": rain["expected"], "reasons": rain["reasons"]},
            "regime": regime(now, rain), "ensemble": ensemble_table(now)}


# --- Ensemble beberapa model, nowcast hujan, peringatan, rezim cuaca ---------------------

ENSEMBLE_MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless", "jma_seamless", "cma_grapes_global"]
MODEL_NAMES = {"ecmwf_ifs025": "ECMWF", "gfs_seamless": "GFS", "icon_seamless": "ICON", "jma_seamless": "JMA",
               "cma_grapes_global": "CMA"}
NOWCAST_URL = "https://data.weather.gov.hk/weatherAPI/hko_data/F3/Gridded_rainfall_nowcast.csv"
WARNSUM_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=warnsum&lang=en"
NEAR_DEG = 0.03      # titik grid nowcast "di stasiun" (±3 km)
AREA_DEG = 0.10      # "sekitar" (±10 km): hujan yang mendekat
RAIN_NEAR_MM = 0.5   # total 2 jam di stasiun yang dianggap hujan
RAIN_AREA_MM = 2.0   # per 30 menit di sekitar yang dianggap hujan mendekat
RAIN_WARNINGS = {"WRAIN": "Hujan lebat (rainstorm)", "WTS": "Badai petir", "WTCSGNL": "Siklon tropis"}
WARNING_NAMES = {**RAIN_WARNINGS, "WHOT": "Sangat panas", "WCOLD": "Dingin", "WMSGNL": "Monsun kuat", "WFIRE": "Bahaya kebakaran",
                 "WFROST": "Embun beku", "WL": "Tanah longsor", "WFNTSA": "Banjir utara", "WTMW": "Tsunami"}


def ensemble_series() -> Dict[str, List[tuple]]:
    """{model: [(utc_datetime, temp_c)]} suhu per jam 3 hari dari beberapa model global (Open-Meteo)."""
    from app.paper_trading.live_market_data import _cached, _http
    from app.paper_trading.weather_outlook import FORECAST_TTL, FORECAST_URL, HKO_COORDS

    lat, lon = HKO_COORDS

    def load():
        url = (f"{FORECAST_URL}?latitude={lat:.4f}&longitude={lon:.4f}&hourly=temperature_2m"
               f"&models={','.join(ENSEMBLE_MODELS)}&timezone=GMT&forecast_days=3")
        h = json.loads(_http(url, timeout=15)).get("hourly") or {}
        times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in h.get("time", [])]
        out = {}
        for model in ENSEMBLE_MODELS:
            values = h.get(f"temperature_2m_{model}") or []
            series = [(t, float(v)) for t, v in zip(times, values) if v is not None]
            if len(series) >= 24:
                out[model] = series
        return out

    try:
        return _cached("hk_ensemble", FORECAST_TTL, load)
    except Exception as err:
        logger.warning("Gagal mengambil ensemble Open-Meteo HK: %s", err)
        return {}


def _interp(series: List[tuple], ts: datetime) -> Optional[float]:
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        if t0 <= ts <= t1:
            return v0 + (v1 - v0) * (ts - t0).total_seconds() / max((t1 - t0).total_seconds(), 1)
    return None


def ensemble_extremes(kind: str, start: datetime, end: datetime, latest_at: Optional[datetime],
                      latest_temp: Optional[float]) -> Dict[str, Dict[str, Any]]:
    """
    Puncak (highest) / lembah (lowest) tiap model dalam [start, end), setelah dikoreksi bacaan HKO terakhir
    (selisih bacaan − model saat itu, meluruh dengan jarak seperti proyeksi per jam). {model: {value, at}}.
    """
    from app.paper_trading.hko_hourly import bias_weight

    out = {}
    for model, series in ensemble_series().items():
        offset = 0.0
        if latest_at is not None and latest_temp is not None:
            at_obs = _interp(series, latest_at)
            if at_obs is not None:
                offset = latest_temp - at_obs
        points = []
        for ts, v in series:
            if start <= ts < end:
                lead = (ts - latest_at).total_seconds() / 3600 if latest_at else 24.0
                points.append((ts, v + offset * bias_weight(lead)))
        if points:
            pick = max if kind == "highest" else min
            ts, value = pick(points, key=lambda p: p[1])
            out[model] = {"value": round(value, 2), "at": ts.astimezone(HKT).isoformat()}
    return out


def nowcast() -> Optional[Dict[str, Any]]:
    """
    Nowcast hujan HKO (radar, 2 jam ke depan per 30 menit) di sekitar stasiun HKO:
    {updated, steps: [{end, near_mm, area_max_mm}], near_total_mm, area_max_mm}. None bila gagal.
    """
    import csv
    import io

    from app.paper_trading.live_market_data import _cached, _http
    from app.paper_trading.weather_outlook import HKO_COORDS

    lat0, lon0 = HKO_COORDS

    def load():
        text = _http(NOWCAST_URL, timeout=20)
        steps: Dict[str, Dict[str, Any]] = {}
        updated = None
        reader = csv.reader(io.StringIO(text))
        next(reader, None)
        for row in reader:
            if len(row) < 5:
                continue
            try:
                lat, lon, mm = float(row[2]), float(row[3]), float(row[4])
            except ValueError:
                continue
            if abs(lat - lat0) > AREA_DEG or abs(lon - lon0) > AREA_DEG:
                continue
            updated = row[0]
            s = steps.setdefault(row[1], {"near": [], "area_max": 0.0})
            s["area_max"] = max(s["area_max"], mm)
            if abs(lat - lat0) <= NEAR_DEG and abs(lon - lon0) <= NEAR_DEG:
                s["near"].append(mm)
        out = []
        for end in sorted(steps):
            s = steps[end]
            out.append({"end": datetime.strptime(end, "%Y%m%d%H%M").replace(tzinfo=HKT).isoformat(),
                        "near_mm": round(sum(s["near"]) / len(s["near"]), 2) if s["near"] else 0.0,
                        "area_max_mm": round(s["area_max"], 2)})
        return {"updated": datetime.strptime(updated, "%Y%m%d%H%M").replace(tzinfo=HKT).isoformat() if updated else None,
                "steps": out, "near_total_mm": round(sum(x["near_mm"] for x in out), 2),
                "area_max_mm": max((x["area_max_mm"] for x in out), default=0.0)}

    try:
        return _cached("hko_nowcast", 10 * 60, load)
    except Exception as err:
        logger.warning("Gagal mengambil nowcast hujan HKO: %s", err)
        return None


def active_warnings() -> List[Dict[str, str]]:
    """Peringatan HKO yang aktif: [{code, name}]."""
    from app.paper_trading.live_market_data import _cached, _http

    def load():
        data = json.loads(_http(WARNSUM_URL) or "{}")
        return [{"code": k, "name": WARNING_NAMES.get(k, k), "detail": str((v or {}).get("name") or (v or {}).get("code") or "")}
                for k, v in (data.items() if isinstance(data, dict) else [])
                if (v or {}).get("actionCode") != "CANCEL"]

    try:
        return _cached("hko_warnsum", 5 * 60, load)
    except Exception as err:
        logger.warning("Gagal mengambil peringatan HKO: %s", err)
        return []


def rain_signal(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Apakah hujan diperkirakan di stasiun HKO dalam ±2 jam: nowcast radar, peringatan hujan/petir, cuaca terkini."""
    reasons = []
    nc = nowcast()
    if nc:
        if nc["near_total_mm"] >= RAIN_NEAR_MM:
            reasons.append(f"nowcast {nc['near_total_mm']:.1f} mm di stasiun (2 jam)")
        elif nc["area_max_mm"] >= RAIN_AREA_MM:
            reasons.append(f"nowcast hujan {nc['area_max_mm']:.1f} mm/30 mnt dalam ±10 km")
    warnings = active_warnings()
    for w in warnings:
        if w["code"] in RAIN_WARNINGS:
            reasons.append(f"peringatan {w['name']}")
    cur = current_weather() or {}
    if cur.get("icon_code") in (62, 63, 64, 65):
        reasons.append(f"sekarang {cur.get('text', '').lower()}")
    return {"expected": bool(reasons), "reasons": reasons, "nowcast": nc, "warnings": warnings}


def cloud_next(now: Optional[datetime] = None, hours: int = 3) -> Dict[str, Optional[float]]:
    """Rata-rata tutupan awan & peluang hujan maksimum beberapa jam ke depan (Open-Meteo)."""
    now = now or datetime.now(timezone.utc)
    rows = [r for r in _open_meteo_hourly() if now <= r["at"] < now + timedelta(hours=hours)]
    clouds = [r["cloud_cover"] for r in rows if r.get("cloud_cover") is not None]
    rains = [r["precipitation_probability"] for r in rows if r.get("precipitation_probability") is not None]
    return {"cloud": round(sum(clouds) / len(clouds), 1) if clouds else None, "rain_prob": max(rains) if rains else None}


def regime(now: Optional[datetime] = None, rain: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Rezim cuaca beberapa jam ke depan untuk kalibrasi: 'hujan', 'mendung', atau 'cerah'."""
    now = now or datetime.now(timezone.utc)
    rain = rain if rain is not None else rain_signal(now)
    cloud = cloud_next(now)
    if rain["expected"] or (cloud["rain_prob"] or 0) >= 60:
        name = "hujan"
    elif (cloud["cloud"] or 0) >= 80:
        name = "mendung"
    else:
        name = "cerah"
    return {"name": name, **cloud, "rain_expected": rain["expected"], "reasons": rain["reasons"]}
