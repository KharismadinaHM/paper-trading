"""
Rekomendasi market suhu berbasis jam puncak lokal.

Market "Highest/Lowest temperature in <kota> on <tanggal>" hanya direkomendasikan pada
jendela waktu menjelang jam puncak suhu di kota tersebut (waktu setempat):

    jendela = [puncak - LEAD - WINDOW, puncak - LEAD)

Default: puncak suhu tertinggi 14:00–15:00 dan terendah 05:00–06:00, LEAD 1 jam, WINDOW
1 jam → rekomendasi highest muncul 12:00–13:00 dan lowest 03:00–04:00 waktu setempat,
hanya untuk market bertanggal hari itu di kota tersebut.

Modul ini murni (tanpa akses database / settlement) agar mudah diuji.
"""
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("weather_peaks")

HIGHEST = "highest"
LOWEST = "lowest"

# Nama kota persis seperti di pertanyaan market Polymarket → zona waktu IANA.
# Tambahan/koreksi bisa lewat CITY_TIMEZONE_OVERRIDES (JSON) tanpa mengubah kode.
CITY_TIMEZONES: Dict[str, str] = {
    "Amsterdam": "Europe/Amsterdam",
    "Ankara": "Europe/Istanbul",
    "Atlanta": "America/New_York",
    "Austin": "America/Chicago",
    "Beijing": "Asia/Shanghai",
    "Boston": "America/New_York",
    "Buenos Aires": "America/Argentina/Buenos_Aires",
    "Busan": "Asia/Seoul",
    "Cape Town": "Africa/Johannesburg",
    "Chengdu": "Asia/Shanghai",
    "Chicago": "America/Chicago",
    "Chongqing": "Asia/Shanghai",
    "Dallas": "America/Chicago",
    "Denver": "America/Denver",
    "Dubai": "Asia/Dubai",
    "Guangzhou": "Asia/Shanghai",
    "Helsinki": "Europe/Helsinki",
    "Hong Kong": "Asia/Hong_Kong",
    "Houston": "America/Chicago",
    "Istanbul": "Europe/Istanbul",
    "Jakarta": "Asia/Jakarta",
    "Jeddah": "Asia/Riyadh",
    "Jinan": "Asia/Shanghai",
    "Karachi": "Asia/Karachi",
    "Kuala Lumpur": "Asia/Kuala_Lumpur",
    "London": "Europe/London",
    "Los Angeles": "America/Los_Angeles",
    "Lucknow": "Asia/Kolkata",
    "Madrid": "Europe/Madrid",
    "Manila": "Asia/Manila",
    "Mexico City": "America/Mexico_City",
    "Miami": "America/New_York",
    "Milan": "Europe/Rome",
    "Moscow": "Europe/Moscow",
    "Mumbai": "Asia/Kolkata",
    "Munich": "Europe/Berlin",
    "New Delhi": "Asia/Kolkata",
    "New York City": "America/New_York",
    "NYC": "America/New_York",
    "Panama City": "America/Panama",
    "Paris": "Europe/Paris",
    "Phoenix": "America/Phoenix",
    "Qingdao": "Asia/Shanghai",
    "San Francisco": "America/Los_Angeles",
    "Sao Paulo": "America/Sao_Paulo",
    "Seattle": "America/Los_Angeles",
    "Seoul": "Asia/Seoul",
    "Seoul (Incheon)": "Asia/Seoul",
    "Shanghai": "Asia/Shanghai",
    "Shenzhen": "Asia/Shanghai",
    "Singapore": "Asia/Singapore",
    "Sydney": "Australia/Sydney",
    "Taipei": "Asia/Taipei",
    "Tel Aviv": "Asia/Jerusalem",
    "Tokyo": "Asia/Tokyo",
    "Toronto": "America/Toronto",
    "Warsaw": "Europe/Warsaw",
    "Washington DC": "America/New_York",
    "Wellington": "Pacific/Auckland",
    "Wuhan": "Asia/Shanghai",
    "Zhengzhou": "Asia/Shanghai",
}

_QUESTION_RE = re.compile(
    r"\b(?P<kind>highest|lowest) temperature in (?P<city>.+?) "
    r"(?:be|reach|exceed|drop|fall|stay|go|hit)\b.*?\bon (?P<month>[A-Z][a-z]+) (?P<day>\d{1,2})\b",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(
    r"\b(?P<kind>highest|lowest) temperature in (?P<city>.+?) on (?P<month>[A-Z][a-z]+) (?P<day>\d{1,2})\b",
    re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TemperatureMarket:
    kind: str          # "highest" / "lowest"
    city: str
    local_date: date   # tanggal market (tanggal lokal di kota tersebut)


@dataclass(frozen=True)
class RecommendationWindow:
    city: str
    kind: str
    tz: ZoneInfo
    start: datetime       # awal jendela rekomendasi (aware, zona lokal)
    end: datetime         # akhir jendela rekomendasi
    peak_start: datetime  # awal jam puncak suhu
    peak_end: datetime

    def contains(self, now: datetime) -> bool:
        return self.start <= now.astimezone(self.tz) < self.end

    def label(self) -> str:
        abbr = self.start.tzname() or ""
        return (f"{self.start:%H:%M}–{self.end:%H:%M} {abbr} · "
                f"puncak {self.peak_start:%H:%M}–{self.peak_end:%H:%M}").strip()


def _json_setting(raw: str, name: str) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        logger.warning("%s bukan JSON valid, diabaikan: %r", name, raw)
        return {}


@lru_cache(maxsize=None)
def _unknown_city_warning(city: str) -> None:
    logger.warning(
        "Zona waktu kota '%s' tidak diketahui; market-nya tidak direkomendasikan. "
        "Tambahkan lewat CITY_TIMEZONE_OVERRIDES.", city,
    )


def city_timezone(city: str) -> Optional[ZoneInfo]:
    overrides = _json_setting(settings.CITY_TIMEZONE_OVERRIDES, "CITY_TIMEZONE_OVERRIDES")
    name = overrides.get(city) or CITY_TIMEZONES.get(city)
    if not name:
        _unknown_city_warning(city)
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Zona waktu '%s' untuk kota '%s' tidak valid.", name, city)
        return None


def _resolve_year(month: int, day: int, reference: Optional[datetime], now: datetime) -> Optional[date]:
    """Tahun diambil dari endDate market jika ada; jika tidak, tahun terdekat dari `now`."""
    if reference is not None:
        ref = reference if reference.tzinfo else reference.replace(tzinfo=timezone.utc)
        candidates = [ref.year, ref.year - 1, ref.year + 1]
    else:
        candidates = [now.year, now.year + 1, now.year - 1]
    for year in candidates:
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if reference is not None or abs((d - now.date()).days) <= 183:
            return d
    return None


def parse_temperature_market(
    name: str,
    reference_date: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> Optional[TemperatureMarket]:
    """Membaca jenis (highest/lowest), kota, dan tanggal dari pertanyaan/judul market suhu."""
    now = now or _utcnow()
    text = str(name or "")
    match = _QUESTION_RE.search(text) or _TITLE_RE.search(text)
    if not match:
        return None
    month = _MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    local_date = _resolve_year(month, int(match.group("day")), reference_date, now)
    if local_date is None:
        return None
    return TemperatureMarket(kind=match.group("kind").lower(), city=match.group("city").strip(), local_date=local_date)


def _peak_start_hour(city: str, kind: str) -> float:
    overrides = _json_setting(settings.TEMP_PEAK_HOUR_OVERRIDES, "TEMP_PEAK_HOUR_OVERRIDES")
    city_override = overrides.get(city) or {}
    if isinstance(city_override, dict) and kind in city_override:
        return float(city_override[kind])
    return float(settings.TEMP_HIGH_PEAK_HOUR if kind == HIGHEST else settings.TEMP_LOW_PEAK_HOUR)


def recommendation_window(city: str, kind: str, local_date: date) -> Optional[RecommendationWindow]:
    tz = city_timezone(city)
    if tz is None:
        return None
    midnight = datetime.combine(local_date, time(0, 0), tzinfo=tz)
    peak_start = midnight + timedelta(hours=_peak_start_hour(city, kind))
    peak_end = peak_start + timedelta(hours=settings.TEMP_PEAK_DURATION_HOURS)
    end = peak_start - timedelta(hours=settings.RECOMMENDATION_LEAD_HOURS)
    start = end - timedelta(hours=settings.RECOMMENDATION_WINDOW_HOURS)
    return RecommendationWindow(city=city, kind=kind, tz=tz, start=start, end=end,
                                peak_start=peak_start, peak_end=peak_end)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _format_duration(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    return f"{minutes // 60}h {minutes % 60:02d}m"


def filter_peak_time_suggestions(
    markets: Iterable[Any],
    min_price: float = 0.70,
    max_price: float = 0.75,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Rekomendasi market suhu yang sedang berada di jendela menjelang jam puncak lokal:
    1. Market suhu highest/lowest yang masih open (menerima order).
    2. Waktu sekarang berada di jendela rekomendasi kota tersebut untuk tanggal market.
    3. Harga YES atau NO berada di rentang [min_price, max_price].
    Diurutkan dari jendela yang paling cepat berakhir.
    """
    now = now or _utcnow()
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    lo, hi = Decimal(str(min_price)), Decimal(str(max_price))
    results: List[Dict[str, Any]] = []

    for m in markets:
        if bool(_get(m, "is_resolved", False)) or str(_get(m, "status", "open")).lower() != "open":
            continue
        name = str(_get(m, "market_name", "") or "")
        parsed = parse_temperature_market(name, _as_datetime(_get(m, "end_date") or _get(m, "resolution_time")), now)
        if parsed is None:
            continue
        window = recommendation_window(parsed.city, parsed.kind, parsed.local_date)
        if window is None or not window.contains(now):
            continue

        price_yes, price_no = _dec(_get(m, "price_yes")), _dec(_get(m, "price_no"))
        if price_yes is not None and lo <= price_yes <= hi:
            side, price = "YES", price_yes
        elif price_no is not None and lo <= price_no <= hi:
            side, price = "NO", price_no
        else:
            continue

        market_id = str(_get(m, "market_id", ""))
        local_now = now.astimezone(window.tz)
        results.append({
            "market_id": market_id,
            "market_name": name,
            "current_price": float(price),
            "side": side,
            "city": parsed.city,
            "kind": parsed.kind,
            "local_date": parsed.local_date.isoformat(),
            "local_time": f"{local_now:%H:%M} {local_now.tzname() or ''}".strip(),
            "window_label": window.label(),
            "window_end": window.end.isoformat(),
            "_sort": window.end.astimezone(timezone.utc),
            "time_remaining": _format_duration(window.end - now),
            "resolution_time": (_as_datetime(_get(m, "resolution_time")) or window.peak_end).isoformat(),
            "outcome_yes_label": _get(m, "outcome_yes_label") or "Yes",
            "outcome_no_label": _get(m, "outcome_no_label") or "No",
            "polymarket_url": _get(m, "polymarket_url") or "",
        })

    # Urutkan berdasarkan waktu absolut (string ISO dengan offset berbeda tidak bisa dibandingkan)
    results.sort(key=lambda r: (r["_sort"], r["city"], -r["current_price"]))
    for r in results:
        r.pop("_sort")
    return results


def upcoming_recommendation_windows(
    markets: Iterable[Any],
    now: Optional[datetime] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """
    Jadwal jendela rekomendasi berikutnya per (kota, jenis) untuk market suhu yang masih open.
    Dipakai dashboard ketika belum ada kota yang sedang berada di jendela.
    """
    now = now or _utcnow()
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    seen: Dict[Tuple[str, str, date], Dict[str, Any]] = {}
    for m in markets:
        if bool(_get(m, "is_resolved", False)) or str(_get(m, "status", "open")).lower() != "open":
            continue
        parsed = parse_temperature_market(str(_get(m, "market_name", "") or ""),
                                          _as_datetime(_get(m, "end_date") or _get(m, "resolution_time")), now)
        if parsed is None:
            continue
        key = (parsed.city, parsed.kind, parsed.local_date)
        if key in seen:
            seen[key]["markets"] += 1
            continue
        window = recommendation_window(parsed.city, parsed.kind, parsed.local_date)
        if window is None or window.end <= now:
            continue
        seen[key] = {
            "city": parsed.city,
            "kind": parsed.kind,
            "local_date": parsed.local_date.isoformat(),
            "window_label": window.label(),
            "starts_at": window.start.isoformat(),
            "starts_in": _format_duration(window.start - now) if window.start > now else "sedang berlangsung",
            "active": window.contains(now),
            "markets": 1,
            "_sort": window.start.astimezone(timezone.utc),
        }
    ordered = sorted(seen.values(), key=lambda w: w["_sort"])[:limit]
    for w in ordered:
        w.pop("_sort")
    return ordered
