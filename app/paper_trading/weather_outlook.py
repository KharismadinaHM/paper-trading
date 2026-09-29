"""
Kondisi cuaca terkini & perkiraan suhu tertinggi/terendah hari ini di stasiun resolusi market.

- Kondisi: dari METAR (tutupan awan FEW/SCT/BKN/OVC + kode cuaca RA/TS/BR/…) atau ikon HKO.
- Tren: kemiringan (least squares) observasi 3 jam terakhir, dalam derajat per jam.
- Perkiraan: prakiraan per jam Open-Meteo di koordinat stasiun, dikoreksi dengan selisih
  observasi terakhir − prakiraan pada jam itu, dan tidak pernah melewati angka yang sudah terukur
  (max perkiraan ≥ max terukur; min perkiraan ≤ min terukur).
Semua ini perkiraan, bukan kepastian.
"""
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.core.logging import get_logger

logger = get_logger("weather_outlook")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_TTL = 30 * 60
HKO_COORDS = (22.302, 114.174)

COVER_CONDITIONS = {
    "CLR": ("☀️", "Cerah"), "SKC": ("☀️", "Cerah"), "CAVOK": ("☀️", "Cerah"), "NSC": ("☀️", "Cerah"),
    "NCD": ("☀️", "Cerah"), "FEW": ("🌤️", "Cerah berawan"), "SCT": ("⛅", "Berawan sebagian"),
    "BKN": ("🌥️", "Berawan"), "OVC": ("☁️", "Mendung"), "OVX": ("☁️", "Mendung"), "VV": ("☁️", "Mendung"),
}
# (kode METAR, emoji, label); urutan = prioritas
WEATHER_CODES = [
    ("TS", "⛈️", "badai petir"), ("SN", "🌨️", "salju"), ("RA", "🌧️", "hujan"), ("DZ", "🌦️", "gerimis"),
    ("FG", "🌫️", "kabut tebal"), ("BR", "🌫️", "berkabut"), ("HZ", "🌫️", "berkabut asap"),
    ("DU", "🌫️", "berdebu"), ("SA", "🌫️", "berdebu"),
]
HKO_ICONS = {
    50: ("☀️", "Cerah"), 51: ("🌤️", "Cerah berawan"), 52: ("⛅", "Berawan sebagian"),
    53: ("🌦️", "Berawan, hujan ringan sesekali"), 54: ("🌦️", "Berawan, hujan sesekali"),
    60: ("🌥️", "Berawan"), 61: ("☁️", "Mendung"), 62: ("🌧️", "Hujan ringan"), 63: ("🌧️", "Hujan"),
    64: ("🌧️", "Hujan lebat"), 65: ("⛈️", "Badai petir"), 76: ("🌥️", "Berawan"), 77: ("🌙", "Cerah"),
    80: ("💨", "Berangin"), 83: ("🌫️", "Berkabut"), 84: ("🌫️", "Berkabut"), 85: ("🌫️", "Berkabut asap"),
    90: ("🔥", "Sangat panas"), 91: ("🌡️", "Hangat"), 92: ("🌡️", "Sejuk"), 93: ("🥶", "Dingin"),
}


def heat_label(temp_c: Optional[float]) -> Optional[str]:
    if temp_c is None:
        return None
    for threshold, label in ((35, "Sangat panas"), (30, "Panas"), (24, "Hangat"), (16, "Sejuk"), (8, "Dingin")):
        if temp_c >= threshold:
            return label
    return "Sangat dingin"


def condition_from_metar(report: Dict[str, Any]) -> Dict[str, Any]:
    """{emoji, label, dim} — dim=True jika awan tebal/hujan (biasanya menahan kenaikan suhu siang)."""
    wx = str(report.get("wxString") or "").upper()
    cover = str(report.get("cover") or "").upper()
    emoji, label = COVER_CONDITIONS.get(cover, ("🌡️", ""))
    extras, emoji_set = [], False
    for code, wx_emoji, wx_label in WEATHER_CODES:  # urutan prioritas: yang pertama menentukan emoji
        if code in wx:
            intensity = "lebat" if f"+{code}" in wx else ("ringan" if f"-{code}" in wx else "")
            extras.append(f"{wx_label} {intensity}".strip())
            if not emoji_set and (code in ("TS", "SN", "RA", "DZ") or not label):
                emoji, emoji_set = wx_emoji, True
    text = ", ".join(x for x in [label] + extras if x) or "Tidak diketahui"
    dim = cover in ("BKN", "OVC", "OVX", "VV") or any(c in wx for c in ("RA", "TS", "DZ", "SN"))
    return {"emoji": emoji, "label": text[:1].upper() + text[1:], "dim": dim}


def condition_from_hko(icon: Optional[int]) -> Optional[Dict[str, Any]]:
    if icon is None or icon not in HKO_ICONS:
        return None
    emoji, label = HKO_ICONS[icon]
    return {"emoji": emoji, "label": label, "dim": icon in (53, 54, 60, 61, 62, 63, 64, 65)}


def temperature_trend(rows: List[Tuple[datetime, float]], now: datetime, hours: float = 3.0,
                      min_span_minutes: int = 55) -> Optional[float]:
    """Kemiringan least squares (derajat/jam) observasi `hours` terakhir; None jika rentang data terlalu pendek."""
    recent = [(ts, t) for ts, t in rows if now - timedelta(hours=hours) <= ts <= now]
    if len(recent) < 2 or (recent[-1][0] - recent[0][0]) < timedelta(minutes=min_span_minutes):
        return None
    xs = [(ts - recent[0][0]).total_seconds() / 3600 for ts, _ in recent]
    ys = [t for _, t in recent]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom, 1)


def fetch_hourly_forecast(lat: float, lon: float) -> List[Tuple[datetime, float]]:
    """[(utc_datetime, temp_c)] prakiraan per jam 2 hari; kosong jika gagal."""
    from app.paper_trading.live_market_data import _cached, _http

    key = f"forecast:{lat:.2f},{lon:.2f}"

    def load():
        url = (f"{FORECAST_URL}?latitude={lat:.4f}&longitude={lon:.4f}&hourly=temperature_2m"
               "&timezone=GMT&forecast_days=2")
        hourly = json.loads(_http(url)).get("hourly") or {}
        return [(datetime.fromisoformat(t).replace(tzinfo=timezone.utc), float(v))
                for t, v in zip(hourly.get("time", []), hourly.get("temperature_2m", [])) if v is not None]

    try:
        return _cached(key, FORECAST_TTL, load)
    except Exception as err:
        logger.warning("Gagal mengambil prakiraan Open-Meteo: %s", err)
        return []


def _forecast_at(forecast: List[Tuple[datetime, float]], ts: datetime) -> Optional[float]:
    """Interpolasi linier prakiraan per jam pada waktu ts."""
    for (t0, v0), (t1, v1) in zip(forecast, forecast[1:]):
        if t0 <= ts <= t1:
            frac = (ts - t0).total_seconds() / max((t1 - t0).total_seconds(), 1)
            return v0 + (v1 - v0) * frac
    return None


def outlook(kind: str, tz: ZoneInfo, local_date: date, observed: Optional[float], observed_at: Optional[datetime],
            current: Optional[float], current_at: Optional[datetime], forecast_c: List[Tuple[datetime, float]],
            unit: str, now: datetime, peak_passed_hint: bool = False) -> Optional[Dict[str, Any]]:
    """
    Perkiraan suhu tertinggi (kind='highest') / terendah hari ini (waktu lokal kota):
    {value, at, passed, source}. passed=True bila puncak kemungkinan sudah lewat.
    peak_passed_hint: jam puncak kota sudah lewat dan suhu tidak naik lagi → max dianggap final
    (mencegah noise prakiraan ±1° sore hari dibaca sebagai puncak baru).
    """
    if observed is None:
        return None
    if kind == "highest" and peak_passed_hint:
        return {"value": observed, "at": observed_at, "passed": True, "reason": "peak", "source": "observasi"}
    convert = (lambda c: c * 9 / 5 + 32) if unit == "F" else (lambda c: c)
    day_end = datetime.combine(local_date, datetime.min.time(), tzinfo=tz) + timedelta(days=1)
    forecast = [(ts, convert(c)) for ts, c in forecast_c]
    offset = 0.0
    if current is not None and current_at is not None:
        at_obs = _forecast_at(forecast, current_at)
        if at_obs is not None:
            offset = current - at_obs  # koreksi bias model terhadap stasiun
    remaining = [(ts, v + offset) for ts, v in forecast if now < ts < day_end]
    if not remaining:
        if not forecast and now < day_end - timedelta(hours=1):
            return None  # prakiraan tidak tersedia: jangan simpulkan puncak sudah lewat
        return {"value": observed, "at": observed_at, "passed": True, "reason": "day_end", "source": "observasi"}
    better = max if kind == "highest" else min
    ts, value = better(remaining, key=lambda r: r[1])
    margin = 0.3 if unit == "C" else 0.5
    beyond = value > observed + margin if kind == "highest" else value < observed - margin
    if not beyond:
        # Sisa hari diperkirakan tidak melampaui angka yang sudah tercatat (mis. max terbawa dari
        # tengah malam); simpan juga puncak prakiraan berikutnya untuk konteks.
        return {"value": observed, "at": observed_at, "passed": True, "reason": "forecast",
                "next_value": round(value, 1), "next_at": ts.astimezone(tz), "source": "observasi"}
    return {"value": round(value, 1), "at": ts.astimezone(tz), "passed": False, "reason": None,
            "source": "Open-Meteo + koreksi observasi"}


def describe_now(unit: str, current: Optional[float], condition: Optional[Dict[str, Any]],
                 trend: Optional[float]) -> str:
    """'☁️ Cuaca sekarang mendung (sejuk); suhu 22°C, naik +0.8°/jam.'"""
    parts = []
    if condition:
        heat = heat_label(current if unit == "C" else (current - 32) * 5 / 9 if current is not None else None)
        parts.append(f"{condition['emoji']} Cuaca sekarang {condition['label'].lower()}"
                     + (f" ({heat.lower()})" if heat else ""))
    if current is not None:
        rate = ""
        if trend is not None:
            direction = "naik" if trend > 0.1 else ("turun" if trend < -0.1 else "stabil")
            rate = f", {direction} {trend:+.1f}°/jam" if direction != "stabil" else ", stabil"
        parts.append(f"suhu {current:g}°{unit}{rate}")
    return "; ".join(parts) + "." if parts else ""


def describe_outlook(kind: str, unit: str, current: Optional[float], condition: Optional[Dict[str, Any]],
                     out: Optional[Dict[str, Any]], now_local: datetime) -> str:
    """Kalimat perkiraan suhu tertinggi/terendah hari ini."""
    if not out:
        return ""
    word = "max" if kind == "highest" else "min"
    at = f" (tercatat {out['at']:%H:%M})" if out.get("at") else ""
    if out["passed"]:
        if out.get("reason") == "forecast" and out.get("next_at"):
            cmp = "tidak melebihi" if kind == "highest" else "tidak di bawah"
            return (f"{word.capitalize()} hari ini kemungkinan tetap {out['value']:g}°{unit}{at}: perkiraan "
                    f"berikutnya ±{out['next_value']:.0f}°{unit} sekitar jam {out['next_at']:%H:%M}, {cmp} angka itu.")
        return f"Puncak kemungkinan sudah lewat — {word} hari ini kemungkinan tetap {out['value']:g}°{unit}{at}."
    hours = max((out["at"] - now_local).total_seconds() / 3600, 0.25)
    text = f"Kemungkinan suhu {word} ±{out['value']:.0f}°{unit} sekitar jam {out['at']:%H:%M}"
    if current is not None:
        delta = out["value"] - current
        text += f", perlu {delta:+.1f}° ({delta / hours:+.1f}°/jam)"
    if kind == "highest" and condition and condition.get("dim"):
        text += " — awan tebal/hujan bisa menahan kenaikan"
    return text + "."


def summarize(kind: str, unit: str, current: Optional[float], condition: Optional[Dict[str, Any]],
              trend: Optional[float], out: Optional[Dict[str, Any]], now_local: datetime) -> str:
    """Kalimat kesimpulan lengkap: kondisi, laju suhu, dan perkiraan puncak hari ini."""
    return " ".join(x for x in (describe_now(unit, current, condition, trend),
                                describe_outlook(kind, unit, current, condition, out, now_local)) if x)
