"""
Market Suggestions Filter Engine.
Murni query & filter data snapshot pasar tanpa kalkulasi keuangan atau settlement.
TIDAK mengimpor atau memanggil modul settlement_engine.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Union


def _get_attr_or_key(obj: Any, key: str, default: Any = None) -> Any:
    """Helper untuk mengambil data baik dari dictionary maupun atribut objek ORM."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _parse_datetime(val: Any) -> Optional[datetime]:
    """Parse berbagai format datetime (datetime object, ISO string, etc.)."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        try:
            # Mengganti Z menjadi +00:00 untuk kompatibilitas fromisoformat Python
            normalized = val.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized)
        except (ValueError, TypeError):
            return None
    return None


def _to_decimal(val: Any) -> Optional[Decimal]:
    """Konversi nilai ke Decimal dengan aman."""
    if val is None:
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError, ValueError):
        return None


def format_time_remaining(seconds: float) -> str:
    """Format total detik ke format terbaca 'Xh YYm'."""
    total_seconds = max(0, int(seconds))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{hours}h {minutes:02d}m"


def filter_market_suggestions(
    markets: Iterable[Any],
    max_hours_to_resolution: float = 6.0,
    min_price: Union[float, Decimal] = 0.70,
    max_price: Union[float, Decimal] = 0.75,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Filter data market snapshot untuk mencari peluang trading (suggestions):
    1. Market belum resolved (status 'open'/'active' dan is_resolved=False).
    2. resolution_time / end_date mendekati waktu sekarang (0 < time_remaining <= max_hours_to_resolution).
    3. Harga YES atau NO berada di rentang [min_price, max_price] (default: 0.70 - 0.75).

    Returns:
        List[Dict] dengan field:
        - market_id
        - market_name
        - current_price
        - resolution_time
        - time_remaining
        - side (opsional: 'YES' / 'NO')
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    dec_min = Decimal(str(min_price))
    dec_max = Decimal(str(max_price))
    max_seconds = max_hours_to_resolution * 3600.0

    suggestions: List[Dict[str, Any]] = []

    for m in markets:
        # 1. Validasi status: Belum resolved & status masih open/active
        is_resolved = _get_attr_or_key(m, "is_resolved", False)
        if bool(is_resolved):
            continue

        raw_status = _get_attr_or_key(m, "status", "open")
        status_str = str(raw_status).strip().lower() if raw_status is not None else "open"
        if status_str in ("resolved", "closed", "settled", "cancelled"):
            continue

        # 2. Validasi waktu: resolution_time / end_date mendekati sekarang
        raw_res_time = _get_attr_or_key(m, "resolution_time")
        if raw_res_time is None:
            raw_res_time = _get_attr_or_key(m, "end_date")

        res_time = _parse_datetime(raw_res_time)
        if res_time is None:
            continue

        # Normalisasi timezone jika naive
        if res_time.tzinfo is None:
            res_time = res_time.replace(tzinfo=timezone.utc)

        time_delta = res_time - now
        seconds_remaining = time_delta.total_seconds()

        # Harus berada di masa depan dan dalam batas max_hours_to_resolution
        if seconds_remaining <= 0 or seconds_remaining > max_seconds:
            continue

        # 3. Validasi harga: YES atau NO di range [min_price, max_price]
        price_yes = _to_decimal(_get_attr_or_key(m, "price_yes"))
        price_no = _to_decimal(_get_attr_or_key(m, "price_no"))
        current_price_raw = _to_decimal(_get_attr_or_key(m, "current_price"))

        matched_price: Optional[Decimal] = None
        matched_side: Optional[str] = None

        if price_yes is not None and dec_min <= price_yes <= dec_max:
            matched_price = price_yes
            matched_side = "YES"
        elif price_no is not None and dec_min <= price_no <= dec_max:
            matched_price = price_no
            matched_side = "NO"
        elif current_price_raw is not None and dec_min <= current_price_raw <= dec_max:
            matched_price = current_price_raw
            explicit_side = _get_attr_or_key(m, "side")
            matched_side = str(explicit_side).upper() if explicit_side else "YES"

        if matched_price is None:
            continue

        # Ambil identifier dan nama market
        market_id = _get_attr_or_key(m, "market_id", "")
        market_name = _get_attr_or_key(
            m, "market_name", _get_attr_or_key(m, "question", _get_attr_or_key(m, "title", ""))
        )

        suggestions.append({
            "market_id": str(market_id),
            "market_name": str(market_name),
            "current_price": float(matched_price),
            "resolution_time": res_time.isoformat(),
            "time_remaining": format_time_remaining(seconds_remaining),
            "side": matched_side,
        })

    return suggestions


def _category_matches(cat_filter: str, cat_lower: str, name_lower: str) -> bool:
    """Mencocokkan filter kategori dengan kolom category dan alias nama pasar."""
    cf = cat_filter.strip().lower()
    if not cf or cf == "all":
        return True
    if cf in cat_lower:
        return True
    if "temp" in cf and any(k in name_lower for k in ("temp", "°f", "°c", "heat", "warm", "cold", "degree", "fahrenheit", "celsius")):
        return True
    if ("rain" in cf or "precip" in cf) and any(k in name_lower for k in ("rain", "precip", "shower", "wet", "rainfall")):
        return True
    if "snow" in cf and any(k in name_lower for k in ("snow", "snowfall", "blizzard")):
        return True
    if ("wind" in cf or "storm" in cf or "hurr" in cf) and any(k in name_lower for k in ("wind", "storm", "hurricane", "cyclone", "tornado")):
        return True
    return False


def search_markets(
    markets: Iterable[Any],
    query: str = "",
    category: Optional[str] = None,
    min_price: Optional[Union[float, Decimal]] = None,
    max_price: Optional[Union[float, Decimal]] = None,
    time_filter: Optional[str] = None,
    sort_by: Optional[str] = "ending_soonest",
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Mencari dan memfilter market dari data snapshot pasar:
    - query: Pencarian parsial case-insensitive pada nama market atau kategori.
    - category: Filter spesifik kategori (Temperature, Precipitation, Snow, Wind / Storm).
    - min_price / max_price: Filter rentang harga (opsional).
    - time_filter: Filter waktu resolusi ('6h', '24h', '3d', '7d', '30d', 'all').
    - sort_by: Pengurutan ('ending_soonest', 'ending_latest', 'highest_price', 'lowest_price', 'name').
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    q = query.strip().lower() if query else ""
    cat_filter = category.strip().lower() if category and category.strip().lower() != "all" else None
    dec_min = Decimal(str(min_price)) if min_price is not None else None
    dec_max = Decimal(str(max_price)) if max_price is not None else None

    # Parsing batas waktu maksimal (dalam detik)
    max_seconds: Optional[float] = None
    if time_filter:
        tf = time_filter.strip().lower()
        if tf in ("6h", "<6h", "< 6h", "6"):
            max_seconds = 6.0 * 3600
        elif tf in ("24h", "<24h", "< 24h", "1d", "today", "24"):
            max_seconds = 24.0 * 3600
        elif tf in ("3d", "<3d", "< 3d", "72h"):
            max_seconds = 3.0 * 86400
        elif tf in ("7d", "<7d", "< 7d", "1w"):
            max_seconds = 7.0 * 86400
        elif tf in ("30d", "<30d", "< 30d", "1m"):
            max_seconds = 30.0 * 86400

    results: List[Dict[str, Any]] = []

    for m in markets:
        market_id = _get_attr_or_key(m, "market_id", "")
        market_name = _get_attr_or_key(
            m, "market_name", _get_attr_or_key(m, "question", _get_attr_or_key(m, "title", ""))
        )
        cat = _get_attr_or_key(m, "category", "Weather")
        raw_status = _get_attr_or_key(m, "status", "open")

        name_lower = str(market_name).lower()
        cat_lower = str(cat).lower()

        # 1. Pencarian keyword
        if q and (q not in name_lower and q not in cat_lower):
            continue

        # 2. Filter kategori fleksibel
        if cat_filter and not _category_matches(cat_filter, cat_lower, name_lower):
            continue

        # 3. Validasi harga
        price_yes = _to_decimal(_get_attr_or_key(m, "price_yes"))
        price_no = _to_decimal(_get_attr_or_key(m, "price_no"))
        curr_price = _to_decimal(_get_attr_or_key(m, "current_price"))

        if curr_price is None and price_yes is not None:
            curr_price = price_yes

        # Filter price range
        if dec_min is not None or dec_max is not None:
            prices_to_check = [p for p in (price_yes, price_no, curr_price) if p is not None]
            if not prices_to_check:
                continue

            matches_range = False
            for p in prices_to_check:
                valid = True
                if dec_min is not None and p < dec_min:
                    valid = False
                if dec_max is not None and p > dec_max:
                    valid = False
                if valid:
                    matches_range = True
                    break

            if not matches_range:
                continue

        # 4. Waktu resolusi & sisa waktu
        raw_res_time = _get_attr_or_key(m, "resolution_time")
        if raw_res_time is None:
            raw_res_time = _get_attr_or_key(m, "end_date")

        res_time = _parse_datetime(raw_res_time)
        res_time_iso = res_time.isoformat() if res_time is not None else None
        time_remaining_str = "-"
        delta_sec = None

        if res_time:
            if res_time.tzinfo is None:
                res_time = res_time.replace(tzinfo=timezone.utc)
            delta_sec = (res_time - now).total_seconds()
            if delta_sec > 0:
                time_remaining_str = format_time_remaining(delta_sec)
            else:
                time_remaining_str = "Passed"

        # Filter waktu jika parameter time_filter aktif
        if max_seconds is not None:
            if delta_sec is None or delta_sec <= 0 or delta_sec > max_seconds:
                continue

        side = _get_attr_or_key(m, "side")
        if not side:
            side = "YES" if (price_yes is not None and price_yes >= (price_no or Decimal("0"))) else "NO"

        results.append({
            "market_id": str(market_id),
            "market_name": str(market_name),
            "category": str(cat),
            "current_price": float(curr_price) if curr_price is not None else None,
            "price_yes": float(price_yes) if price_yes is not None else None,
            "price_no": float(price_no) if price_no is not None else None,
            "resolution_time": res_time_iso,
            "time_remaining": time_remaining_str,
            "_delta_sec": delta_sec if (delta_sec is not None and delta_sec > 0) else 999999999,
            "status": str(raw_status),
            "side": str(side),
        })

    # 5. Sorting
    sb = str(sort_by).lower() if sort_by else "ending_soonest"
    if sb in ("ending_soonest", "ending_soon"):
        results.sort(key=lambda x: x.get("_delta_sec", 999999999))
    elif sb == "ending_latest":
        results.sort(key=lambda x: x.get("_delta_sec", -1), reverse=True)
    elif sb in ("highest_price", "price_desc"):
        results.sort(key=lambda x: (x.get("current_price") or 0.0), reverse=True)
    elif sb in ("lowest_price", "price_asc"):
        results.sort(key=lambda x: (x.get("current_price") or 999.0))
    elif sb == "name":
        results.sort(key=lambda x: x.get("market_name", "").lower())

    # Bersihkan internal sorting key
    for r in results:
        r.pop("_delta_sec", None)

    return results

