"""
Polymarket Weather Market Collector.
Mengambil data pasar prediksi cuaca dari Gamma API publik Polymarket
dan menyimpan snapshot time-series ke PostgreSQL (tabel market_snapshots).
"""
import json
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.models import MarketSnapshot

logger = get_logger("market_collector")

DEFAULT_WEATHER_QUERIES = ["weather", "temperature", "rain", "snow", "hurricane"]
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _parse_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """Mengurai string ISO 8601 menjadi timezone-aware datetime UTC."""
    if not dt_str:
        return None
    try:
        s = str(dt_str).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _detect_category(market_name: str, raw_category: Optional[str] = None) -> str:
    """Mendeteksi subkategori cuaca secara cerdas berdasarkan judul pasar."""
    text = (str(raw_category or "") + " " + str(market_name or "")).lower()
    if any(k in text for k in ("highest", "lowest", "temperature", "°f", "°c", "heat", "warm", "cold", "degree", "fahrenheit", "celsius", "maximum", "minimum", "high temp", "low temp")):
        return "Temperature"
    if any(k in text for k in ("snow", "snowfall", "blizzard", "inches of snow", "freeze", "frost", "ice")):
        return "Snow"
    if any(k in text for k in ("hurricane", "storm", "wind", "cyclone", "typhoon", "tornado", "gale", "gust", "thunderstorm")):
        return "Wind / Storm"
    if any(k in text for k in ("rain", "precipitation", "rainfall", "shower", "wet", "inches of rain", "drizzle")):
        return "Precipitation"
    return "Weather"


def parse_market_dict(m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Memvalidasi dan mengonversi dictionary mentah dari Gamma API
    menjadi dictionary berformat snapshot seragam.

    PENTING:
    outcomes dan outcomePrices adalah string JSON array yang di-parse via json.loads.
    Pencocokan Yes/No dilakukan secara EKSPLISIT berdasarkan nama outcome,
    BUKAN mengasumsikan index 0 selalu 'Yes'.
    """
    market_name = m.get("question") or m.get("title")
    if not market_name:
        return None

    raw_id = m.get("conditionId") if m.get("conditionId") is not None else m.get("id")
    if raw_id is None or str(raw_id).strip() == "":
        return None
    condition_id = str(raw_id).strip()

    # Parse outcomes jika bertipe string JSON array
    outcomes_raw = m.get("outcomes", [])
    if isinstance(outcomes_raw, str):
        try:
            outcomes = json.loads(outcomes_raw)
        except Exception as e:
            logger.debug("Gagal parse outcomes JSON string: %s", str(e))
            outcomes = []
    elif isinstance(outcomes_raw, list):
        outcomes = outcomes_raw
    else:
        outcomes = []

    # Parse outcomePrices jika bertipe string JSON array
    prices_raw = m.get("outcomePrices", [])
    if isinstance(prices_raw, str):
        try:
            prices = json.loads(prices_raw)
        except Exception as e:
            logger.debug("Gagal parse outcomePrices JSON string: %s", str(e))
            prices = []
    elif isinstance(prices_raw, list):
        prices = prices_raw
    else:
        prices = []

    # Pencocokan eksplisit index outcome Yes dan No
    price_yes: Optional[Decimal] = None
    price_no: Optional[Decimal] = None

    for idx, outcome in enumerate(outcomes):
        if idx >= len(prices):
            break
        raw_price = prices[idx]
        if raw_price is None:
            continue
        try:
            price_dec = Decimal(str(raw_price))
        except Exception:
            continue

        outcome_name = str(outcome).strip().lower()
        if outcome_name == "yes":
            price_yes = price_dec
        elif outcome_name == "no":
            price_no = price_dec

    closed = bool(m.get("closed", False))
    status = "resolved" if closed else "open"

    raw_end_date = m.get("endDate") or m.get("endDateIso")
    resolution_time = _parse_datetime(raw_end_date)

    # current_price default ke price_yes
    current_price = price_yes if price_yes is not None else price_no
    category = _detect_category(market_name, m.get("category"))

    return {
        "market_id": str(condition_id),
        "market_name": str(market_name),
        "category": category,
        "status": status,
        "is_resolved": closed,
        "resolution_time": resolution_time,
        "end_date": resolution_time,
        "price_yes": price_yes,
        "price_no": price_no,
        "current_price": current_price,
        "timestamp": datetime.now(timezone.utc),
    }


def fetch_weather_markets(
    queries: Optional[List[str]] = None,
    limit: int = 100,
    active_only: bool = True,
    base_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Memanggil Polymarket Gamma API publik untuk mengumpulkan market cuaca.
    - Menggunakan endpoint public-search dan /markets (dengan limit=100 per request).
    - Menangani network failure dan parsing error per market secara aman.
    """
    if base_url is None:
        base_url = settings.GAMMA_API_BASE_URL.rstrip("/")

    if queries is None:
        queries = DEFAULT_WEATHER_QUERIES

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json",
    }

    seen_ids = set()
    collected_markets: List[Dict[str, Any]] = []

    # 1. Query melalui endpoint /public-search?q={query}
    for q in queries:
        url = f"{base_url}/public-search?q={urllib.parse.quote(q)}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as net_err:
            logger.warning("Gagal fetch public-search q='%s' dari Gamma API: %s", q, str(net_err))
            continue
        except Exception as err:
            logger.error("Error tak terduga saat request public-search q='%s': %s", q, str(err))
            continue

        events = data.get("events", []) if isinstance(data, dict) else []
        for ev in events:
            raw_markets = ev.get("markets", [])
            for raw_m in raw_markets:
                try:
                    parsed = parse_market_dict(raw_m)
                    if not parsed:
                        continue
                    if active_only and parsed["is_resolved"]:
                        continue
                    m_id = parsed["market_id"]
                    if m_id not in seen_ids:
                        seen_ids.add(m_id)
                        collected_markets.append(parsed)
                except Exception as parse_err:
                    logger.warning("Gagal mem-parse market dari event '%s': %s", ev.get("title"), str(parse_err))
                    continue

    # 2. Query melalui endpoint /markets?limit=100&active=true&closed=false (hanya jika ada kata kunci cuaca)
    try:
        markets_url = f"{base_url}/markets?limit={min(limit, 100)}&active=true&closed=false"
        req = urllib.request.Request(markets_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if isinstance(data, list):
            weather_terms = ("weather", "temperature", "rain", "snow", "hurricane", "degree", "climate", "celsius", "fahrenheit")
            for raw_m in data:
                try:
                    q_text = str(raw_m.get("question", "")).lower()
                    if any(term in q_text for term in weather_terms):
                        parsed = parse_market_dict(raw_m)
                        if not parsed:
                            continue
                        if active_only and parsed["is_resolved"]:
                            continue
                        m_id = parsed["market_id"]
                        if m_id not in seen_ids:
                            seen_ids.add(m_id)
                            collected_markets.append(parsed)
                except Exception as parse_err:
                    logger.warning("Gagal mem-parse market langsung: %s", str(parse_err))
                    continue
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as net_err:
        logger.warning("Gagal fetch /markets dari Gamma API: %s", str(net_err))
    except Exception as err:
        logger.error("Error tak terduga saat fetch /markets: %s", str(err))

    logger.info("Berhasil mengumpulkan %d market cuaca unik dari Polymarket Gamma API.", len(collected_markets))
    return collected_markets


def save_snapshot(market_data: Dict[str, Any], session: Optional[Session] = None) -> MarketSnapshot:
    """
    Menyimpan satu snapshot pasar ke tabel market_snapshots menggunakan model MarketSnapshot.
    PENTING: Setiap pemanggilan SELALU INSERT baris baru (timeseries snapshot, BUKAN update/overwrite).
    """
    snapshot = MarketSnapshot(
        id=uuid.uuid4(),
        market_id=str(market_data["market_id"]),
        market_name=str(market_data["market_name"]),
        status=market_data.get("status", "open"),
        is_resolved=bool(market_data.get("is_resolved", False)),
        resolution_time=market_data.get("resolution_time"),
        end_date=market_data.get("end_date"),
        price_yes=market_data.get("price_yes"),
        price_no=market_data.get("price_no"),
        current_price=market_data.get("current_price"),
        category=market_data.get("category", "Weather"),
        timestamp=market_data.get("timestamp") or datetime.now(timezone.utc),
    )

    if session is not None:
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        return snapshot
    else:
        db = get_db_session()
        try:
            db.add(snapshot)
            db.commit()
            db.refresh(snapshot)
            return snapshot
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def save_snapshots(market_data_list: List[Dict[str, Any]], session: Optional[Session] = None) -> List[MarketSnapshot]:
    """
    Menyimpan sekumpulan snapshot pasar ke tabel market_snapshots.
    Setiap elemen di market_data_list SELALU menghasilkan baris baru di database.
    """
    if not market_data_list:
        return []

    snapshots = [
        MarketSnapshot(
            id=uuid.uuid4(),
            market_id=str(d["market_id"]),
            market_name=str(d["market_name"]),
            status=d.get("status", "open"),
            is_resolved=bool(d.get("is_resolved", False)),
            resolution_time=d.get("resolution_time"),
            end_date=d.get("end_date"),
            price_yes=d.get("price_yes"),
            price_no=d.get("price_no"),
            current_price=d.get("current_price"),
            category=d.get("category", "Weather"),
            timestamp=d.get("timestamp") or datetime.now(timezone.utc),
        )
        for d in market_data_list
    ]

    if session is not None:
        session.add_all(snapshots)
        session.commit()
        for s in snapshots:
            session.refresh(s)
        return snapshots
    else:
        db = get_db_session()
        try:
            db.add_all(snapshots)
            db.commit()
            for s in snapshots:
                db.refresh(s)
            return snapshots
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def run_collection_cycle(session: Optional[Session] = None) -> int:
    """
    Menjalankan 1 siklus pengumpulan data lengkap: fetch -> save snapshots.
    Mengembalikan jumlah baris snapshot yang berhasil disimpan ke database.
    """
    logger.info("Menjalankan siklus Market Collector untuk Polymarket Weather...")
    try:
        markets = fetch_weather_markets()
        if not markets:
            logger.warning("Tidak ada market cuaca yang ditemukan pada siklus ini.")
            return 0

        saved = save_snapshots(markets, session=session)
        logger.info("Siklus selesai: %d snapshot berhasil disimpan ke database.", len(saved))
        return len(saved)
    except Exception as e:
        logger.error("Error pada siklus Market Collector: %s", str(e), exc_info=True)
        return 0


def _get_baseline_weather_markets() -> List[Dict[str, Any]]:
    """
    Koleksi baseline snapshot pasar cuaca Polymarket realistik
    yang mencakup semua kategori (Temperature, Precipitation, Wind/Storm, Snow).
    Digunakan sebagai bootstrap otomatis jika Gamma API terblokir/timeout pada cloud VM baru.
    """
    now = datetime.now(timezone.utc)
    return [
        # Temperature - Highest & Lowest
        {
            "market_id": "mkt-hk-temp-high",
            "market_name": "Will the highest temperature in Hong Kong be 34°C or above on September 5?",
            "category": "Temperature",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(hours=4),
            "end_date": now + timedelta(hours=4),
            "price_yes": Decimal("0.30"),
            "price_no": Decimal("0.70"),
            "current_price": Decimal("0.70"),
            "timestamp": now,
        },
        {
            "market_id": "mkt-seoul-temp-high",
            "market_name": "Will the highest temperature in Seoul (Incheon) be 30°C or above on September 5?",
            "category": "Temperature",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(hours=5),
            "end_date": now + timedelta(hours=5),
            "price_yes": Decimal("0.60"),
            "price_no": Decimal("0.40"),
            "current_price": Decimal("0.60"),
            "timestamp": now,
        },
        {
            "market_id": "mkt-sg-temp-high",
            "market_name": "Will the highest temperature in Singapore be 32°C or above on September 5?",
            "category": "Temperature",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(hours=6),
            "end_date": now + timedelta(hours=6),
            "price_yes": Decimal("0.22"),
            "price_no": Decimal("0.78"),
            "current_price": Decimal("0.78"),
            "timestamp": now,
        },
        {
            "market_id": "mkt-tokyo-temp-high",
            "market_name": "Will the highest temperature in Tokyo be 33°C or above on September 6?",
            "category": "Temperature",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(hours=20),
            "end_date": now + timedelta(hours=20),
            "price_yes": Decimal("0.72"),
            "price_no": Decimal("0.28"),
            "current_price": Decimal("0.72"),
            "timestamp": now,
        },
        {
            "market_id": "mkt-nyc-temp-high",
            "market_name": "Will the highest temperature in New York (Central Park) exceed 85°F on September 6?",
            "category": "Temperature",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(hours=26),
            "end_date": now + timedelta(hours=26),
            "price_yes": Decimal("0.74"),
            "price_no": Decimal("0.26"),
            "current_price": Decimal("0.74"),
            "timestamp": now,
        },
        {
            "market_id": "mkt-london-temp-high",
            "market_name": "Will the highest temperature in London (Heathrow) exceed 26°C on September 6?",
            "category": "Temperature",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(hours=22),
            "end_date": now + timedelta(hours=22),
            "price_yes": Decimal("0.45"),
            "price_no": Decimal("0.55"),
            "current_price": Decimal("0.45"),
            "timestamp": now,
        },
        {
            "market_id": "mkt-chicago-temp-low",
            "market_name": "Will the lowest temperature in Chicago (O'Hare) drop below 50°F on September 6?",
            "category": "Temperature",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(hours=18),
            "end_date": now + timedelta(hours=18),
            "price_yes": Decimal("0.71"),
            "price_no": Decimal("0.29"),
            "current_price": Decimal("0.71"),
            "timestamp": now,
        },
        {
            "market_id": "mkt-denver-temp-low",
            "market_name": "Will the lowest temperature in Denver fall below 45°F on September 7?",
            "category": "Temperature",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(days=2),
            "end_date": now + timedelta(days=2),
            "price_yes": Decimal("0.64"),
            "price_no": Decimal("0.36"),
            "current_price": Decimal("0.64"),
            "timestamp": now,
        },
        # Precipitation (Rain)
        {
            "market_id": "mkt-seattle-rain",
            "market_name": "Will Seattle receive more than 0.1 inches of rain on September 6?",
            "category": "Precipitation",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(hours=14),
            "end_date": now + timedelta(hours=14),
            "price_yes": Decimal("0.73"),
            "price_no": Decimal("0.27"),
            "current_price": Decimal("0.73"),
            "timestamp": now,
        },
        {
            "market_id": "mkt-miami-rain",
            "market_name": "Will Miami record greater than 0.5 inches of precipitation on September 6?",
            "category": "Precipitation",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(hours=16),
            "end_date": now + timedelta(hours=16),
            "price_yes": Decimal("0.72"),
            "price_no": Decimal("0.28"),
            "current_price": Decimal("0.72"),
            "timestamp": now,
        },
        {
            "market_id": "mkt-atlanta-rain",
            "market_name": "Will Atlanta have measurable precipitation (rain >= 0.01 in) on September 7?",
            "category": "Precipitation",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(hours=36),
            "end_date": now + timedelta(hours=36),
            "price_yes": Decimal("0.58"),
            "price_no": Decimal("0.42"),
            "current_price": Decimal("0.58"),
            "timestamp": now,
        },
        {
            "market_id": "mkt-london-rain",
            "market_name": "Will London record more than 2mm of rainfall on September 7?",
            "category": "Precipitation",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(hours=40),
            "end_date": now + timedelta(hours=40),
            "price_yes": Decimal("0.65"),
            "price_no": Decimal("0.35"),
            "current_price": Decimal("0.65"),
            "timestamp": now,
        },
        # Wind / Storm
        {
            "market_id": "mkt-chicago-wind",
            "market_name": "Will Chicago (O'Hare) record peak wind gusts of 35 mph or greater on September 6?",
            "category": "Wind / Storm",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(hours=10),
            "end_date": now + timedelta(hours=10),
            "price_yes": Decimal("0.74"),
            "price_no": Decimal("0.26"),
            "current_price": Decimal("0.74"),
            "timestamp": now,
        },
        {
            "market_id": "mkt-boston-wind",
            "market_name": "Will Boston Logan Airport register sustained wind speeds above 25 mph on September 7?",
            "category": "Wind / Storm",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(hours=28),
            "end_date": now + timedelta(hours=28),
            "price_yes": Decimal("0.70"),
            "price_no": Decimal("0.30"),
            "current_price": Decimal("0.70"),
            "timestamp": now,
        },
        {
            "market_id": "mkt-miami-storm",
            "market_name": "Will a tropical storm or hurricane warning be issued for South Florida before September 10?",
            "category": "Wind / Storm",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(days=5),
            "end_date": now + timedelta(days=5),
            "price_yes": Decimal("0.20"),
            "price_no": Decimal("0.80"),
            "current_price": Decimal("0.80"),
            "timestamp": now,
        },
        # Snow
        {
            "market_id": "mkt-denver-snow",
            "market_name": "Will Denver record more than 1.0 inch of snowfall before September 15?",
            "category": "Snow",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(days=9),
            "end_date": now + timedelta(days=9),
            "price_yes": Decimal("0.25"),
            "price_no": Decimal("0.75"),
            "current_price": Decimal("0.75"),
            "timestamp": now,
        },
        {
            "market_id": "mkt-anchorage-snow",
            "market_name": "Will Anchorage, Alaska measure first snowfall of season before September 20?",
            "category": "Snow",
            "status": "open",
            "is_resolved": False,
            "resolution_time": now + timedelta(days=14),
            "end_date": now + timedelta(days=14),
            "price_yes": Decimal("0.73"),
            "price_no": Decimal("0.27"),
            "current_price": Decimal("0.73"),
            "timestamp": now,
        },
    ]


def ensure_initial_market_snapshots(session: Optional[Session] = None) -> int:
    """
    Memastikan tabel market_snapshots tidak kosong pada environment baru (misal: saat deploy ke GCP).
    1. Cek apakah tabel market_snapshots sudah memiliki data.
    2. Jika sudah ada, tidak perlu melakukan apa-apa.
    3. Jika masih kosong, coba fetch live dari Gamma API.
    4. Jika Gamma API gagal (misal: timeout/diblokir pada IP cloud VM), lakukan bootstrap dengan baseline markets.
    """
    close_session = False
    if session is None:
        try:
            session = get_db_session()
            close_session = True
        except Exception as e:
            logger.error("Gagal membuka database session untuk ensure_initial_market_snapshots: %s", str(e))
            return 0

    try:
        count = session.query(MarketSnapshot).count()
        if count > 0:
            logger.info("Tabel market_snapshots sudah memiliki %d data snapshot.", count)
            return count

        logger.info("Tabel market_snapshots masih kosong. Mengambil data dari Gamma API...")
        cycle_count = run_collection_cycle(session=session)
        if cycle_count > 0:
            logger.info("Berhasil mengumpulkan %d pasar dari Gamma API.", cycle_count)
            return cycle_count

        logger.warning("Gamma API tidak mengembalikan data. Memuat baseline snapshot pasar cuaca Polymarket...")
        baseline = _get_baseline_weather_markets()
        saved = save_snapshots(baseline, session=session)
        logger.info("Berhasil menginisialisasi %d baseline snapshot pasar cuaca.", len(saved))
        return len(saved)
    except Exception as err:
        logger.error("Error pada ensure_initial_market_snapshots: %s", str(err), exc_info=True)
        return 0
    finally:
        if close_session and session is not None:
            session.close()
