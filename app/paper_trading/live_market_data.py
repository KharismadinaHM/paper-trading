"""
Data live untuk rekomendasi market suhu:
- Order book CLOB (harga ask/bid asli) → saran memakai harga yang benar-benar bisa dibeli dan
  melewati bracket tidak likuid. Harga Gamma/mid "50¢" sering berasal dari order book kosong
  (bid 3¢ / ask 97¢), bukan harga yang bisa diperdagangkan.
- Observasi stasiun resolusi (METAR bandara via aviationweather.gov; HKO untuk Hong Kong) →
  suhu tertinggi/terendah yang sudah terukur sejak tengah malam lokal + suhu terakhir.

Semua fetch di-cache singkat dan gagal dengan aman (data kosong, rekomendasi tetap jalan).
"""
import csv
import io
import json
import math
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("live_market_data")

CLOB_BOOKS_URL = "https://clob.polymarket.com/books"
METAR_URL = "https://aviationweather.gov/api/data/metar"
HKO_MAXMIN_URL = "https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather/latest_since_midnight_maxmin.csv"
HKO_CURRENT_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=rhrread&lang=en"
HEADERS = {"User-Agent": "Mozilla/5.0 (paper-trading)", "Accept": "application/json", "Content-Type": "application/json"}

BOOK_TTL = 60          # detik
OBS_TTL = 5 * 60

_cache: Dict[str, Any] = {}
_lock = threading.Lock()


def _cached(key: str, ttl: float, loader):
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = loader()
    with _lock:
        _cache[key] = (now, value)
    return value


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _http(url: str, data: Optional[bytes] = None, timeout: int = 10) -> str:
    req = urllib.request.Request(url, data=data, headers=HEADERS, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


# --- Order book ---------------------------------------------------------------------------

def _summarize_book(book: Dict[str, Any]) -> Dict[str, Optional[float]]:
    asks = [(float(a["price"]), float(a["size"])) for a in book.get("asks") or []]
    bids = [(float(b["price"]), float(b["size"])) for b in book.get("bids") or []]
    best_ask = min(asks, default=None)
    best_bid = max(bids, default=None)
    return {
        "ask": best_ask[0] if best_ask else None,
        "ask_size": best_ask[1] if best_ask else None,
        "bid": best_bid[0] if best_bid else None,
    }


def fetch_order_books(token_ids: Iterable[str]) -> Dict[str, Dict[str, Optional[float]]]:
    """{token_id: {ask, ask_size, bid}}; kosong jika CLOB tidak bisa dihubungi."""
    tokens = sorted({t for t in token_ids if t})
    if not tokens:
        return {}

    def load():
        out: Dict[str, Dict[str, Optional[float]]] = {}
        for i in range(0, len(tokens), 100):
            chunk = tokens[i:i + 100]
            body = json.dumps([{"token_id": t} for t in chunk]).encode()
            for book in json.loads(_http(CLOB_BOOKS_URL, data=body)):
                out[str(book.get("asset_id"))] = _summarize_book(book)
        return out

    try:
        return _cached("books:" + ",".join(tokens), BOOK_TTL, load)
    except Exception as err:
        logger.warning("Gagal mengambil order book CLOB: %s", err)
        return {}


def apply_liquidity(event: Dict[str, Any], books: Dict[str, Dict[str, Optional[float]]]) -> None:
    """
    Tandai tiap bracket dengan ask/bid/spread & likuid, lalu jadikan bracket likuid dengan peluang
    tertinggi sebagai saran utama. Tanpa data order book, urutan & saran tidak diubah.
    """
    max_spread = settings.RECOMMENDATION_MAX_SPREAD
    have_books = False
    for m in event["markets"]:
        book = books.get(str(m.get("yes_token_id"))) if m.get("yes_token_id") else None
        if book is None:
            m.update(ask=None, bid=None, spread=None, liquid=None)
            continue
        have_books = True
        ask, bid = book["ask"], book["bid"]
        spread = round(ask - bid, 4) if ask is not None and bid is not None else None
        m.update(ask=ask, bid=bid, ask_size=book.get("ask_size"), spread=spread,
                 liquid=spread is not None and spread <= max_spread)
    if not have_books:
        event["liquid"] = None
        return
    liquid = [m for m in event["markets"] if m.get("liquid")]
    event["liquid"] = bool(liquid)
    if liquid:
        top = liquid[0]  # markets sudah urut dari peluang (mid) tertinggi
        event["markets"].remove(top)
        event["markets"].insert(0, top)


def entry_price(market: Dict[str, Any]) -> Optional[float]:
    """Harga beli: ask order book jika tersedia, selain itu harga mid/Gamma."""
    return market.get("ask") if market.get("ask") is not None else market.get("price_yes")


# --- Observasi stasiun --------------------------------------------------------------------

def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_metar_observations(stations: Iterable[str]) -> Dict[str, List[tuple]]:
    """{ICAO: [(utc_datetime, temp_c), ...]} 30 jam terakhir."""
    ids = sorted({s for s in stations if s and s != "HKO"})
    if not ids:
        return {}

    def load():
        out: Dict[str, List[tuple]] = {}
        for i in range(0, len(ids), 40):
            url = f"{METAR_URL}?ids={','.join(ids[i:i + 40])}&hours=30&format=json"
            for row in json.loads(_http(url) or "[]"):
                if row.get("temp") is None or not row.get("reportTime"):
                    continue
                out.setdefault(str(row["icaoId"]).upper(), []).append((_parse_time(row["reportTime"]), float(row["temp"])))
        for rows in out.values():
            rows.sort()
        return out

    try:
        return _cached("metar:" + ",".join(ids), OBS_TTL, load)
    except Exception as err:
        logger.warning("Gagal mengambil METAR: %s", err)
        return {}


def fetch_hko_observation() -> Optional[Dict[str, Any]]:
    """Maks/min sejak tengah malam & suhu terkini stasiun Hong Kong Observatory."""
    def load():
        result: Dict[str, Any] = {}
        for row in csv.reader(io.StringIO(_http(HKO_MAXMIN_URL))):
            # Di CSV maks/min namanya "HK Observatory" (di data terkini "Hong Kong Observatory")
            if len(row) >= 4 and row[1].strip().lower() in ("hk observatory", "hong kong observatory"):
                result.update(at=datetime.strptime(row[0], "%Y%m%d%H%M").replace(tzinfo=ZoneInfo("Asia/Hong_Kong")),
                              max=float(row[2]), min=float(row[3]))
        try:
            current = json.loads(_http(HKO_CURRENT_URL))
            for item in (current.get("temperature") or {}).get("data", []):
                if item.get("place") == "Hong Kong Observatory":
                    result["current"] = float(item["value"])
        except Exception:
            pass
        return result or None

    try:
        return _cached("hko", OBS_TTL, load)
    except Exception as err:
        logger.warning("Gagal mengambil observasi HKO: %s", err)
        return None


def _to_unit(temp_c: float, unit: str) -> float:
    # °F dibulatkan ke derajat bulat seperti tabel NOAA & bracket market (60.08 → 60)
    return float(math.floor(temp_c * 9 / 5 + 32 + 0.5)) if unit == "F" else temp_c


def observed_extreme(kind: str, unit: str, tz: ZoneInfo, local_date: date,
                     rows: List[tuple]) -> Optional[Dict[str, Any]]:
    """Suhu tertinggi/terendah terukur sejak tengah malam lokal + observasi terakhir."""
    start = datetime.combine(local_date, datetime.min.time(), tzinfo=tz)
    end = start + timedelta(days=1)
    today = [(ts, _to_unit(t, unit)) for ts, t in rows if start <= ts.astimezone(tz) < end]
    if not today:
        return None
    pick = max if kind == "highest" else min
    at, value = pick(today, key=lambda r: r[1])
    last_at, last = today[-1]
    return {"value": round(value, 1), "at": at.astimezone(tz), "current": round(last, 1),
            "current_at": last_at.astimezone(tz), "unit": unit}


def station_url(station: Optional[str]) -> Optional[str]:
    """Halaman sumber resolusi: NOAA timeseries per stasiun ICAO, atau halaman cuaca terkini HKO."""
    if not station:
        return None
    if station == "HKO":
        return "https://www.hko.gov.hk/en/wxinfo/currwx/current.htm"
    return f"https://www.weather.gov/wrh/timeseries?site={station.lower()}"


def station_source(station: Optional[str]) -> str:
    return "HKO" if station == "HKO" else "NOAA"


def station_day_summary(station: str, tz: ZoneInfo, unit: str, local_date: Optional[date] = None,
                        now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """
    Suhu terkini + tertinggi/terendah sejak tengah malam lokal di stasiun resolusi.
    {station, source, url, unit, current, current_at, max, max_at, min, min_at}; None tanpa data.
    """
    now = now or datetime.now(timezone.utc)
    local_date = local_date or now.astimezone(tz).date()
    base = {"station": station, "source": station_source(station), "url": station_url(station), "unit": unit}
    if station == "HKO":
        hko = fetch_hko_observation()
        if not hko or not hko.get("at") or hko["at"].date() != local_date:
            return None
        return {**base, "unit": "C", "current": hko.get("current"), "current_at": hko["at"],
                "max": hko.get("max"), "max_at": None, "min": hko.get("min"), "min_at": None}
    rows = fetch_metar_observations([station]).get(station, [])
    high = observed_extreme("highest", unit, tz, local_date, rows)
    if high is None:
        return None
    low = observed_extreme("lowest", unit, tz, local_date, rows)
    return {**base, "current": high["current"], "current_at": high["current_at"],
            "max": high["value"], "max_at": high["at"], "min": low["value"], "min_at": low["at"]}


def event_unit(event: Dict[str, Any]) -> str:
    return "F" if any("°F" in str(m.get("bracket") or "") for m in event["markets"]) else "C"


def apply_observations(events: List[Dict[str, Any]]) -> None:
    """Tambahkan event['observation'] = {station, value, at, current, current_at, unit} bila ada."""
    from app.paper_trading.weather_peaks import city_timezone

    metar = fetch_metar_observations(e.get("station") for e in events)
    hko = fetch_hko_observation() if any(e.get("station") == "HKO" for e in events) else None
    for e in events:
        e["observation"] = None
        station, tz = e.get("station"), city_timezone(e["city"])
        if not station or tz is None:
            continue
        local_date = date.fromisoformat(e["local_date"])
        if station == "HKO":
            if hko and hko.get("at") and hko["at"].date() == local_date:
                e["observation"] = {
                    "station": "HKO", "value": hko["max" if e["kind"] == "highest" else "min"], "at": None,
                    "current": hko.get("current"), "current_at": hko["at"], "unit": "C",
                    "source": "HKO", "url": station_url("HKO"),
                }
            continue
        obs = observed_extreme(e["kind"], event_unit(e), tz, local_date, metar.get(station, []))
        if obs:
            e["observation"] = {"station": station, "source": "NOAA", "url": station_url(station), **obs}


def enrich_suggestions(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Order book + observasi stasiun untuk event rekomendasi aktif (dipanggil saat request)."""
    if not events:
        return events
    try:
        books = fetch_order_books(m.get("yes_token_id") for e in events for m in e["markets"])
        for e in events:
            apply_liquidity(e, books)
        apply_observations(events)
    except Exception as err:  # data live tidak boleh menggagalkan rekomendasi
        logger.warning("Gagal menambahkan data live ke rekomendasi: %s", err, exc_info=True)
    return events

