"""
Polymarket Weather Market Collector.
Mengambil data pasar prediksi cuaca dari Gamma API publik Polymarket
dan menyimpan snapshot time-series ke PostgreSQL (tabel market_snapshots).
"""
import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.models import (
    MARKET_DATA_FIELDS,
    MarketLatest,
    MarketResolution,
    MarketSnapshot,
    PaperPosition,
    upsert_market_latest,
)

logger = get_logger("market_collector")

# In-memory cache for weather events (TTL 60 seconds)
_weather_events_cache: Dict[str, Any] = {"timestamp": 0.0, "events": []}

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
    - Menggunakan endpoint public-search dan /events per tag cuaca (paginasi, limit=100 per halaman).
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

    # 2. Paginasi event bertag cuaca (/events?tag_id=...&offset=...) agar cakupan tidak
    #    terbatas pada 100 hasil pertama
    page_size = max(1, min(limit, 100))
    tag_ids = [t.strip() for t in str(settings.WEATHER_TAG_IDS).split(",") if t.strip()]
    for tag_id in tag_ids:
        for page in range(max(1, settings.COLLECTOR_MAX_PAGES)):
            events_url = (
                f"{base_url}/events?limit={page_size}&offset={page * page_size}"
                f"&active=true&closed=false&tag_id={urllib.parse.quote(tag_id)}"
            )
            try:
                req = urllib.request.Request(events_url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as net_err:
                logger.warning("Gagal fetch events tag_id=%s offset=%d: %s", tag_id, page * page_size, net_err)
                break
            except Exception as err:
                logger.error("Error tak terduga saat fetch events tag_id=%s: %s", tag_id, err)
                break

            events = data if isinstance(data, list) else []
            for ev in events:
                for raw_m in ev.get("markets", []) or []:
                    try:
                        parsed = parse_market_dict(raw_m)
                        if not parsed or (active_only and parsed["is_resolved"]):
                            continue
                        if parsed["market_id"] not in seen_ids:
                            seen_ids.add(parsed["market_id"])
                            collected_markets.append(parsed)
                    except Exception as parse_err:
                        logger.warning("Gagal mem-parse market dari event '%s': %s", ev.get("title"), parse_err)
            if len(events) < page_size:
                break

    logger.info("Berhasil mengumpulkan %d market cuaca unik dari Polymarket Gamma API.", len(collected_markets))
    return collected_markets


def fetch_weather_events(
    date_filter: Optional[str] = None,
    base_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Mengambil data event cuaca "Highest/Lowest Temperature" dari Polymarket Gamma API
    dalam format grouped-by-event (per kota + tanggal).
    
    Fitur:
    - Multi-endpoint parallel fetch (tag Weather, Daily Temperature, Highest Temperature)
    - Caching in-memory (TTL 60 detik) untuk performa responsif
    - Ekstraksi harga/probabilitas akurat dari lastTradePrice & outcomePrices
    - Sub-markets diurutkan berdasarkan probabilitas (pct_yes) tertinggi (menyerupai kartu Polymarket)
    - Support filter by date (format YYYY-MM-DD)
    """
    # 1. Cek cache in-memory jika masih valid (TTL 60 detik)
    now_ts = time.time()
    cached_events = _weather_events_cache.get("events", [])
    cache_ts = _weather_events_cache.get("timestamp", 0.0)

    if cached_events and (now_ts - cache_ts < 60):
        if date_filter:
            return [e for e in cached_events if e.get("end_date") == date_filter]
        return cached_events

    if base_url is None:
        base_url = settings.GAMMA_API_BASE_URL.rstrip("/")

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json",
    }

    # Endpoint multi-tag untuk menjaring semua event cuaca (hari ini, besok, lusa)
    endpoints = [
        f"{base_url}/events?limit=100&active=true&closed=false&tag_id=84&order=volume24hr&ascending=false",
        f"{base_url}/events?limit=100&active=true&closed=false&tag_id=103040&order=startDate&ascending=false",
        f"{base_url}/events?limit=100&active=true&closed=false&tag_id=103040&order=volume24hr&ascending=false",
        f"{base_url}/events?limit=100&active=true&closed=false&tag_id=104596&order=startDate&ascending=false",
        f"{base_url}/public-search?q=Highest+temperature",
        f"{base_url}/public-search?q=Lowest+temperature",
    ]

    def _fetch_url(target_url: str) -> List[Dict[str, Any]]:
        try:
            req = urllib.request.Request(target_url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    return data
                elif isinstance(data, dict):
                    return data.get("events", [])
        except Exception as net_err:
            logger.warning("Gagal fetch dari %s: %s", target_url, str(net_err))
        return []

    # Eksekusi parallel fetch
    with ThreadPoolExecutor(max_workers=min(len(endpoints), 6)) as executor:
        responses = list(executor.map(_fetch_url, endpoints))

    seen_event_ids = set()
    all_events: List[Dict[str, Any]] = []

    for raw_events in responses:
        for ev in raw_events:
            event_id = ev.get("id")
            if not event_id or event_id in seen_event_ids:
                continue

            title = ev.get("title", "")
            title_lower = title.lower()
            if "temperature" not in title_lower:
                continue

            # Skip jika event closed dan tidak aktif
            if ev.get("closed", False) and not ev.get("active", True):
                continue

            seen_event_ids.add(event_id)

            # Parse end_date event
            raw_end = ev.get("endDate") or ""
            end_date_str = ""
            if raw_end:
                parsed_end = _parse_datetime(raw_end)
                if parsed_end:
                    end_date_str = parsed_end.strftime("%Y-%m-%d")

            # Fallback: parse tanggal dari judul (misal "on September 9?")
            if not end_date_str:
                m_date = re.search(
                    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})",
                    title,
                    re.IGNORECASE,
                )
                if m_date:
                    mon_name = m_date.group(1).capitalize()
                    day_num = int(m_date.group(2))
                    cur_year = datetime.now(timezone.utc).year
                    try:
                        dt = datetime.strptime(f"{mon_name} {day_num} {cur_year}", "%B %d %Y")
                        end_date_str = dt.strftime("%Y-%m-%d")
                    except Exception:
                        pass

            # Parse sub-markets
            raw_markets = ev.get("markets", [])
            sub_markets: List[Dict[str, Any]] = []

            for raw_m in raw_markets:
                if raw_m.get("closed", False):
                    continue

                question = raw_m.get("question", "")
                group_item_title = raw_m.get("groupItemTitle", "")
                condition_id = raw_m.get("conditionId", "")

                price_yes = None
                price_no = None

                # Sumber harga SAMA dengan yang dipakai eksekusi order (outcomePrices, dicocokkan
                # eksplisit ke outcome 'Yes'), agar harga di kartu tidak memicu divergence palsu.
                parsed_prices = parse_market_dict({**raw_m, "question": question or "-", "conditionId": condition_id or "-"})
                if parsed_prices and parsed_prices.get("price_yes") is not None:
                    price_yes = round(float(parsed_prices["price_yes"]), 4)
                    price_no = round(1.0 - price_yes, 4)

                # Fallback ke lastTradePrice jika outcomePrices belum tersedia
                last_trade = raw_m.get("lastTradePrice")
                if price_yes is None and last_trade is not None:
                    try:
                        lt = float(last_trade)
                        if 0 <= lt <= 1:
                            price_yes = round(lt, 4)
                            price_no = round(1.0 - lt, 4)
                    except (ValueError, TypeError):
                        pass

                # Persentase Yes / No
                pct_yes = round(price_yes * 100) if price_yes is not None else 0
                pct_no = round(price_no * 100) if price_no is not None else 0

                vol_num = 0
                try:
                    vol_num = float(raw_m.get("volumeNum", 0) or 0)
                except (ValueError, TypeError):
                    vol_num = 0

                sub_markets.append({
                    "condition_id": str(condition_id),
                    "question": str(question),
                    "group_item_title": str(group_item_title),
                    "price_yes": price_yes,
                    "price_no": price_no,
                    "pct_yes": pct_yes,
                    "pct_no": pct_no,
                    "volume": vol_num,
                })

            if not sub_markets:
                continue

            # PENTING: Urutkan sub-markets berdasarkan probabilitas (pct_yes) tertinggi!
            # Ini memastikan kandidat suhu teratas (misal 51%, 39%) tampil di kartu,
            # bukan bracket bersuhu rendah yang berpeluang 0%.
            sub_markets.sort(key=lambda x: (x.get("pct_yes") or 0), reverse=True)

            # Parse total volume event
            total_volume = 0
            try:
                total_volume = float(ev.get("volume", 0) or 0)
            except (ValueError, TypeError):
                total_volume = 0

            # Format volume display
            if total_volume >= 1_000_000:
                vol_display = f"${total_volume / 1_000_000:.1f}M"
            elif total_volume >= 1_000:
                vol_display = f"${total_volume / 1_000:.0f}K"
            else:
                vol_display = f"${total_volume:.0f}"

            # Determine if event is new (created within last 48h)
            created_at = _parse_datetime(ev.get("creationDate") or ev.get("createdAt"))
            is_new = False
            if created_at:
                age_hours = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600
                is_new = age_hours < 48

            # Build Polymarket URL
            slug = ev.get("slug", "")
            poly_url = f"https://polymarket.com/event/{slug}" if slug else ""

            all_events.append({
                "event_id": str(event_id),
                "title": str(title),
                "image": ev.get("image") or ev.get("icon") or "",
                "slug": str(slug),
                "polymarket_url": poly_url,
                "volume": total_volume,
                "volume_display": vol_display,
                "end_date": end_date_str,
                "is_new": is_new,
                "is_active": bool(ev.get("active", True)),
                "markets": sub_markets,
            })

    # Sort events: aktif terlebih dahulu, lalu berdasarkan volume terbesar
    all_events.sort(key=lambda e: (not e["is_active"], -e["volume"], e["end_date"]))

    # Simpan ke cache module
    _weather_events_cache["timestamp"] = time.time()
    _weather_events_cache["events"] = all_events

    logger.info("Berhasil mengumpulkan %d weather events dari Polymarket Gamma API.", len(all_events))

    if date_filter:
        return [e for e in all_events if e.get("end_date") == date_filter]

    return all_events


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


# Field yang menentukan apakah observasi baru perlu dicatat sebagai baris histori
_CHANGE_FIELDS = ("market_name", "status", "is_resolved", "resolution_time", "price_yes", "price_no", "category")


def _differs(a: Any, b: Any) -> bool:
    if isinstance(a, datetime) and isinstance(b, datetime):
        a = a if a.tzinfo else a.replace(tzinfo=timezone.utc)
        b = b if b.tzinfo else b.replace(tzinfo=timezone.utc)
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) != bool(b)
    if isinstance(a, (Decimal, int, float)) and isinstance(b, (Decimal, int, float)):
        return Decimal(str(a)) != Decimal(str(b))
    return a != b


def record_market_observations(
    markets: List[Dict[str, Any]],
    session: Optional[Session] = None,
    now: Optional[datetime] = None,
) -> Dict[str, int]:
    """
    Mencatat hasil satu siklus collector secara hemat:
    - market_latest SELALU diperbarui (harga & waktu observasi terbaru → cek stale tetap akurat).
    - Baris histori baru di market_snapshots hanya ditulis jika harga/status berubah, atau
      heartbeat SNAPSHOT_HEARTBEAT_SECONDS sudah lewat sejak baris histori terakhir.
    """
    now = now or datetime.now(timezone.utc)
    heartbeat = timedelta(seconds=max(0, settings.SNAPSHOT_HEARTBEAT_SECONDS))
    close_session = session is None
    db = session or get_db_session()
    try:
        ids = [m["market_id"] for m in markets]
        existing: Dict[str, MarketLatest] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            existing.update({r.market_id: r for r in db.query(MarketLatest).filter(MarketLatest.market_id.in_(chunk))})

        history_rows: List[Dict[str, Any]] = []
        refresh_rows: List[Dict[str, Any]] = []
        for m in markets:
            m = {**m, "timestamp": m.get("timestamp") or now}
            prev = existing.get(m["market_id"])
            last_hist = prev.last_snapshot_at if prev is not None else None
            if last_hist is not None and last_hist.tzinfo is None:
                last_hist = last_hist.replace(tzinfo=timezone.utc)
            changed = prev is None or any(_differs(m.get(f), getattr(prev, f)) for f in _CHANGE_FIELDS)
            if changed or last_hist is None or m["timestamp"] - last_hist >= heartbeat:
                history_rows.append(m)
            else:
                refresh_rows.append({f: m.get(f) for f in MARKET_DATA_FIELDS})

        if history_rows:
            save_snapshots(history_rows, session=db)  # market_latest ikut diperbarui oleh listener
        if refresh_rows:
            for i in range(0, len(refresh_rows), 500):
                upsert_market_latest(db.connection(), refresh_rows[i:i + 500], history_written=False)
            db.commit()
        return {"observed": len(markets), "history_rows": len(history_rows)}
    except Exception:
        db.rollback()
        raise
    finally:
        if close_session:
            db.close()


def run_collection_cycle(session: Optional[Session] = None, now: Optional[datetime] = None) -> int:
    """
    Menjalankan 1 siklus pengumpulan data lengkap: fetch -> catat observasi.
    Market yang waktu resolusinya sudah lewat dilewati (tidak bisa di-trade); posisi terbuka
    pada market tersebut tetap diperbarui oleh settlement worker.
    Mengembalikan jumlah market yang berhasil diamati.
    """
    now = now or datetime.now(timezone.utc)
    logger.info("Menjalankan siklus Market Collector untuk Polymarket Weather...")
    try:
        markets = fetch_weather_markets()
        markets = [
            m for m in markets
            if m.get("resolution_time") is None or m["resolution_time"] > now
        ]
        if not markets:
            logger.warning("Tidak ada market cuaca yang ditemukan pada siklus ini.")
            return 0

        result = record_market_observations(markets, session=session, now=now)
        logger.info(
            "Siklus selesai: %d market diamati, %d baris histori baru.",
            result["observed"], result["history_rows"],
        )
        return result["observed"]
    except Exception as e:
        logger.error("Error pada siklus Market Collector: %s", str(e), exc_info=True)
        return 0


def _get_baseline_weather_markets() -> List[Dict[str, Any]]:
    """
    Koleksi baseline snapshot pasar cuaca SINTETIS (bukan market Polymarket asli)
    yang mencakup semua kategori (Temperature, Precipitation, Wind/Storm, Snow).
    Hanya dimuat jika ALLOW_SYNTHETIC_MARKETS=true (demo / development lokal).
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

        if not settings.ALLOW_SYNTHETIC_MARKETS:
            logger.warning(
                "Gamma API tidak mengembalikan data dan ALLOW_SYNTHETIC_MARKETS=false. "
                "Tabel market_snapshots dibiarkan kosong sampai collector berhasil mengambil data asli."
            )
            return 0

        logger.warning("Gamma API tidak mengembalikan data. Memuat baseline snapshot pasar SINTETIS (demo)...")
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


def determine_winning_outcome(raw_market: Dict[str, Any]) -> Optional[str]:
    """
    Menentukan outcome pemenang dari market Gamma API yang sudah closed.
    - Outcome dengan harga final 1 -> pemenang ('YES' / 'NO').
    - Harga final 50/50 -> 'INVALID' (modal dikembalikan).
    - Market belum closed / harga belum final -> None (belum bisa di-settle).
    """
    if not bool(raw_market.get("closed", False)):
        return None
    uma_status = str(raw_market.get("umaResolutionStatus") or "resolved").lower()
    if uma_status not in ("resolved", "finalized"):
        return None

    parsed = parse_market_dict({**raw_market, "question": raw_market.get("question") or "-",
                                "conditionId": raw_market.get("conditionId") or raw_market.get("id") or "-"})
    if not parsed:
        return None
    price_yes, price_no = parsed.get("price_yes"), parsed.get("price_no")
    if price_yes is None or price_no is None:
        return None
    if price_yes >= Decimal("0.99") and price_no <= Decimal("0.01"):
        return "YES"
    if price_no >= Decimal("0.99") and price_yes <= Decimal("0.01"):
        return "NO"
    if abs(price_yes - Decimal("0.5")) <= Decimal("0.01") and abs(price_no - Decimal("0.5")) <= Decimal("0.01"):
        return "INVALID"
    return None


def fetch_markets_by_condition_ids(
    condition_ids: List[str],
    base_url: Optional[str] = None,
    chunk_size: int = 20,
) -> List[Dict[str, Any]]:
    """
    Mengambil data market mentah dari Gamma API berdasarkan conditionId, termasuk market
    yang sudah closed (Gamma secara default menyaring market closed, jadi diquery dua kali).
    """
    if base_url is None:
        base_url = settings.GAMMA_API_BASE_URL.rstrip("/")
    headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"}

    found: Dict[str, Dict[str, Any]] = {}
    ids = [c for c in dict.fromkeys(condition_ids) if c]
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        params = "&".join(f"condition_ids={urllib.parse.quote(c)}" for c in chunk)
        for closed_param in ("", "&closed=true"):
            url = f"{base_url}/markets?limit={len(chunk)}&{params}{closed_param}"
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except Exception as err:
                logger.warning("Gagal fetch market by condition_ids dari Gamma API: %s", err)
                continue
            for raw_m in data if isinstance(data, list) else []:
                cid = raw_m.get("conditionId")
                if cid:
                    found[str(cid)] = raw_m
    return list(found.values())


def sync_markets_by_condition_ids(condition_ids: List[str], session: Optional[Session] = None) -> Dict[str, int]:
    """
    Menyimpan snapshot terbaru (termasuk market closed) untuk condition id tertentu dan
    mencatat hasil resolusi ke tabel market_resolutions.
    """
    raw_markets = fetch_markets_by_condition_ids(condition_ids)
    snapshots: List[Dict[str, Any]] = []
    resolutions: List[Dict[str, Any]] = []
    for raw_m in raw_markets:
        parsed = parse_market_dict(raw_m)
        if not parsed:
            continue
        winner = determine_winning_outcome(raw_m)
        if parsed["is_resolved"] and winner is None:
            # Market sudah ditutup tapi hasil belum final: tetap tandai belum resolved
            # agar tidak di-settle dengan harga yang belum pasti.
            parsed["is_resolved"] = False
            parsed["status"] = "closed"
        snapshots.append(parsed)
        if winner:
            resolutions.append({"market_id": parsed["market_id"], "market_name": parsed["market_name"],
                                "winning_outcome": winner})

    close_session = session is None
    db = session or get_db_session()
    try:
        if snapshots:
            save_snapshots(snapshots, session=db)
        for r in resolutions:
            if db.get(MarketResolution, r["market_id"]) is None:
                db.add(MarketResolution(**r, resolved_at=datetime.now(timezone.utc)))
                logger.info("Market %s resolved: %s", r["market_id"], r["winning_outcome"])
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        if close_session:
            db.close()
    return {"snapshots": len(snapshots), "resolutions": len(resolutions)}


def sync_open_position_markets(session: Optional[Session] = None) -> Dict[str, int]:
    """Memperbarui harga & status resolusi untuk semua market yang masih punya posisi terbuka."""
    close_session = session is None
    db = session or get_db_session()
    try:
        market_ids = [row[0] for row in db.query(PaperPosition.market_id).filter(PaperPosition.shares > 0).distinct()]
    finally:
        if close_session:
            db.close()
    if not market_ids:
        return {"snapshots": 0, "resolutions": 0}
    return sync_markets_by_condition_ids(market_ids, session=session)


def prune_market_snapshots(
    retention_days: Optional[int] = None,
    session: Optional[Session] = None,
    now: Optional[datetime] = None,
) -> int:
    """
    Retensi time-series: menghapus snapshot yang lebih tua dari `retention_days` hari,
    KECUALI snapshot terbaru setiap market (tetap dibutuhkan untuk harga & histori posisi).
    Mengembalikan jumlah baris yang dihapus.
    """
    from sqlalchemy import func

    days = settings.SNAPSHOT_RETENTION_DAYS if retention_days is None else retention_days
    if not days or days <= 0:
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)

    close_session = session is None
    db = session or get_db_session()
    try:
        latest = (
            db.query(MarketSnapshot.market_id, func.max(MarketSnapshot.timestamp).label("max_ts"))
            .group_by(MarketSnapshot.market_id)
            .subquery()
        )
        keep_ids = (
            db.query(MarketSnapshot.id)
            .join(latest, (MarketSnapshot.market_id == latest.c.market_id)
                  & (MarketSnapshot.timestamp == latest.c.max_ts))
        )
        deleted = (
            db.query(MarketSnapshot)
            .filter(MarketSnapshot.timestamp < cutoff, MarketSnapshot.id.not_in(keep_ids))
            .delete(synchronize_session=False)
        )
        # market_latest: buang market yang sudah lama tidak diamati dan tidak punya posisi terbuka
        open_ids = db.query(PaperPosition.market_id).filter(PaperPosition.shares > 0)
        db.query(MarketLatest).filter(
            MarketLatest.timestamp < cutoff, MarketLatest.market_id.not_in(open_ids)
        ).delete(synchronize_session=False)
        db.commit()
        if deleted:
            logger.info("Retensi snapshot: %d baris lebih tua dari %d hari dihapus.", deleted, days)
        return deleted
    except Exception:
        db.rollback()
        raise
    finally:
        if close_session:
            db.close()
