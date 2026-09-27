"""
Rekomendasi market suhu berbasis jam puncak lokal tiap kota.

Jam puncak dihitung per kota & per tanggal (waktu setempat):
- Suhu tertinggi: solar noon tanggal itu + median lag kota (hasil riset data per jam historis)
- Suhu terendah : matahari terbit tanggal itu + median lag kota
Sumber lag: app/paper_trading/peak_calibration.json (dibuat oleh scripts/research_peak_hours.py).
Karena berbasis posisi matahari, pergeseran musim dan DST ikut terhitung otomatis.

Puncak = jendela `TEMP_PEAK_DURATION_HOURS` yang berpusat di waktu puncak tersebut, dan
rekomendasi muncul pada:

    jendela = [awal puncak - LEAD - WINDOW, awal puncak - LEAD)

Hanya market bertanggal hari itu di kota tersebut yang direkomendasikan. Tanpa filter harga
(opsional lewat min_price / max_price). Modul ini murni (tanpa database / settlement).
"""
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings
from app.core.logging import get_logger
from app.paper_trading.cities import CITIES, CITY_ALIASES, resolve_city
from app.paper_trading.solar import solar_noon_utc, sunrise_utc

logger = get_logger("weather_peaks")

HIGHEST = "highest"
LOWEST = "lowest"
CALIBRATION_FILE = Path(__file__).resolve().parent / "peak_calibration.json"

# Lag default jika kota belum punya kalibrasi (rata-rata umum: max ±2 jam setelah solar noon,
# min sekitar matahari terbit)
DEFAULT_LAG_HOURS = {HIGHEST: 2.0, LOWEST: 0.0}

# Kompatibilitas: nama kota → zona waktu IANA (termasuk alias judul event)
CITY_TIMEZONES: Dict[str, str] = {
    **{name: city.tz for name, city in CITIES.items()},
    **{alias: CITIES[target].tz for alias, target in CITY_ALIASES.items()},
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
_BRACKET_RE = re.compile(r"\bbe (?:between )?(?P<bracket>.+?) on [A-Z][a-z]+ \d{1,2}\b", re.IGNORECASE)
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
    peak_start: datetime  # awal jendela puncak suhu
    peak_end: datetime
    source: str           # "data" (kalibrasi riset), "model" (default), "override" (konfigurasi)

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


@lru_cache(maxsize=1)
def load_calibration() -> Dict[str, Dict[str, Any]]:
    """Lag puncak per kota hasil riset (kosong jika file tidak ada)."""
    try:
        return json.loads(CALIBRATION_FILE.read_text()).get("cities", {})
    except (OSError, json.JSONDecodeError) as err:
        logger.warning("Kalibrasi jam puncak tidak dapat dibaca (%s); memakai lag default.", err)
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


def bracket_label(name: str) -> str:
    """Rentang suhu dari pertanyaan market, mis. '31°C or higher' atau '60-61°F'."""
    match = _BRACKET_RE.search(str(name or ""))
    return match.group("bracket").strip() if match else str(name or "")


def _round_to_quarter(dt: datetime) -> datetime:
    minutes = round((dt.minute + dt.second / 60) / 15) * 15
    return dt.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minutes)


def peak_center(city: str, kind: str, local_date: date) -> Optional[Tuple[datetime, str]]:
    """
    Perkiraan waktu suhu tertinggi/terendah (zona lokal) untuk kota & tanggal tersebut.
    Prioritas: TEMP_PEAK_HOUR_OVERRIDES → kalibrasi riset → model default (solar) → jam default.
    """
    tz = city_timezone(city)
    if tz is None:
        return None
    canonical = resolve_city(city)
    half_peak = timedelta(hours=settings.TEMP_PEAK_DURATION_HOURS / 2)

    overrides = _json_setting(settings.TEMP_PEAK_HOUR_OVERRIDES, "TEMP_PEAK_HOUR_OVERRIDES")
    city_override = overrides.get(city) or overrides.get(canonical) or {}
    if isinstance(city_override, dict) and kind in city_override:
        # Override menyatakan AWAL jam puncak (jam lokal); kembalikan titik tengahnya
        start = datetime.combine(local_date, time(0, 0), tzinfo=tz) + timedelta(hours=float(city_override[kind]))
        return start + half_peak, "override"

    info = CITIES.get(canonical)
    if info is None:
        # Kota tanpa koordinat (mis. hanya ditambahkan lewat CITY_TIMEZONE_OVERRIDES)
        hour = settings.TEMP_HIGH_PEAK_HOUR if kind == HIGHEST else settings.TEMP_LOW_PEAK_HOUR
        start = datetime.combine(local_date, time(0, 0), tzinfo=tz) + timedelta(hours=float(hour))
        return start + half_peak, "model"

    lag = load_calibration().get(canonical, {}).get("lag_max_hours" if kind == HIGHEST else "lag_min_hours")
    source = "data" if lag is not None else "model"
    if lag is None:
        lag = DEFAULT_LAG_HOURS[kind]

    if kind == HIGHEST:
        anchor = solar_noon_utc(local_date, info.lon)
    else:
        anchor = sunrise_utc(local_date, info.lat, info.lon) or solar_noon_utc(local_date, info.lon) - timedelta(hours=6)
    return _round_to_quarter((anchor + timedelta(hours=float(lag))).astimezone(tz)), source


def recommendation_window(city: str, kind: str, local_date: date) -> Optional[RecommendationWindow]:
    center = peak_center(city, kind, local_date)
    if center is None:
        return None
    peak_mid, source = center
    peak_start = peak_mid - timedelta(hours=settings.TEMP_PEAK_DURATION_HOURS / 2)
    peak_end = peak_start + timedelta(hours=settings.TEMP_PEAK_DURATION_HOURS)
    end = peak_start - timedelta(hours=settings.RECOMMENDATION_LEAD_HOURS)
    start = end - timedelta(hours=settings.RECOMMENDATION_WINDOW_HOURS)
    return RecommendationWindow(city=city, kind=kind, tz=peak_mid.tzinfo, start=start, end=end,
                                peak_start=peak_start, peak_end=peak_end, source=source)


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


def _in_price_range(m: Any, min_price: Optional[float], max_price: Optional[float]) -> bool:
    if min_price is None and max_price is None:
        return True
    lo = Decimal(str(min_price)) if min_price is not None else Decimal("0")
    hi = Decimal(str(max_price)) if max_price is not None else Decimal("1")
    return any(p is not None and lo <= p <= hi for p in (_dec(_get(m, "price_yes")), _dec(_get(m, "price_no"))))


def _open_temperature_markets(markets: Iterable[Any], now: datetime):
    for m in markets:
        if bool(_get(m, "is_resolved", False)) or str(_get(m, "status", "open")).lower() != "open":
            continue
        name = str(_get(m, "market_name", "") or "")
        parsed = parse_temperature_market(name, _as_datetime(_get(m, "end_date") or _get(m, "resolution_time")), now)
        if parsed is not None:
            yield m, name, parsed


def filter_peak_time_suggestions(
    markets: Iterable[Any],
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Rekomendasi per EVENT (kota + jenis + tanggal) yang sedang berada di jendela menjelang
    jam puncak lokal. Setiap event berisi semua bracket-nya, diurutkan dari peluang YES
    tertinggi. Filter harga opsional (default: tidak ada).
    """
    now = now or _utcnow()
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    events: Dict[Tuple[str, str, date], Dict[str, Any]] = {}
    outside: set = set()

    for m, name, parsed in _open_temperature_markets(markets, now):
        key = (resolve_city(parsed.city), parsed.kind, parsed.local_date)
        if key in outside:
            continue
        event = events.get(key)
        if event is None:
            window = recommendation_window(parsed.city, parsed.kind, parsed.local_date)
            if window is None or not window.contains(now):
                outside.add(key)
                continue
            local_now = now.astimezone(window.tz)
            event = events[key] = {
                "event_key": f"{key[0]}|{parsed.kind}|{parsed.local_date.isoformat()}",
                "city": key[0],
                "kind": parsed.kind,
                "local_date": parsed.local_date.isoformat(),
                "local_time": f"{local_now:%H:%M} {local_now.tzname() or ''}".strip(),
                "window_label": window.label(),
                "window_end": window.end.isoformat(),
                "peak_start": window.peak_start.isoformat(),
                "peak_source": window.source,
                "time_remaining": _format_duration(window.end - now),
                "markets": [],
                "_sort": window.end.astimezone(timezone.utc),
            }
        if not _in_price_range(m, min_price, max_price):
            continue
        price_yes, price_no = _dec(_get(m, "price_yes")), _dec(_get(m, "price_no"))
        event["markets"].append({
            "market_id": str(_get(m, "market_id", "")),
            "market_name": name,
            "bracket": bracket_label(name),
            "price_yes": float(price_yes) if price_yes is not None else None,
            "price_no": float(price_no) if price_no is not None else None,
            "outcome_yes_label": _get(m, "outcome_yes_label") or "Yes",
            "outcome_no_label": _get(m, "outcome_no_label") or "No",
            "volume": _float_or_none(_get(m, "volume")),
            "polymarket_url": _get(m, "polymarket_url") or "",
        })

    results = [e for e in events.values() if e["markets"]]
    for e in results:
        e["markets"].sort(key=lambda x: -(x["price_yes"] or 0))
        e["market_count"] = len(e["markets"])
        e["volume"] = sum(x["volume"] or 0 for x in e["markets"])
    # Urutkan berdasarkan waktu absolut (string ISO dengan offset berbeda tidak bisa dibandingkan)
    results.sort(key=lambda e: (e["_sort"], e["city"], e["kind"]))
    for e in results:
        e.pop("_sort")
    return results


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def top_cities_by_volume(markets: Iterable[Any], limit: int, now: Optional[datetime] = None) -> Optional[List[str]]:
    """
    Kota dengan total volume market suhu open terbesar (semua tanggal, tertinggi + terendah),
    urut dari yang terbesar. Mengembalikan None jika belum ada data volume sama sekali (mis.
    collector belum sempat mengisi kolom volume) supaya pemanggil tidak menyaring semuanya.
    """
    now = now or _utcnow()
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    totals: Dict[str, float] = {}
    for m, _, parsed in _open_temperature_markets(markets, now):
        city = resolve_city(parsed.city)
        totals[city] = totals.get(city, 0.0) + (_float_or_none(_get(m, "volume")) or 0.0)
    if not any(totals.values()):
        return None
    ranked = sorted(totals, key=lambda c: (-totals[c], c))
    return ranked[:limit] if limit > 0 else ranked


def upcoming_recommendation_windows(
    markets: Iterable[Any],
    now: Optional[datetime] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """
    Jadwal jendela rekomendasi berikutnya per (kota, jenis, tanggal) untuk market suhu yang
    masih open. Dipakai dashboard ketika belum ada kota yang sedang berada di jendela.
    """
    now = now or _utcnow()
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    seen: Dict[Tuple[str, str, date], Dict[str, Any]] = {}
    skipped: set = set()
    for _, _, parsed in _open_temperature_markets(markets, now):
        key = (resolve_city(parsed.city), parsed.kind, parsed.local_date)
        if key in skipped:
            continue
        if key in seen:
            seen[key]["markets"] += 1
            continue
        window = recommendation_window(parsed.city, parsed.kind, parsed.local_date)
        if window is None or window.end <= now:
            skipped.add(key)
            continue
        seen[key] = {
            "city": key[0],
            "kind": parsed.kind,
            "local_date": parsed.local_date.isoformat(),
            "window_label": window.label(),
            "peak_source": window.source,
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
