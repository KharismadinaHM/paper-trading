"""
Polymarket Weather Market Collector.
Mengambil data pasar prediksi cuaca dari Gamma API publik Polymarket
dan menyimpan snapshot time-series ke PostgreSQL (tabel market_snapshots).
"""
import json
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
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
