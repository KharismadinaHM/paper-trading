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


def search_markets(
    markets: Iterable[Any],
    query: str = "",
    category: Optional[str] = None,
    min_price: Optional[Union[float, Decimal]] = None,
    max_price: Optional[Union[float, Decimal]] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Mencari dan memfilter market dari data snapshot pasar:
    - query: Pencarian parsial case-insensitive pada nama market atau kategori.
    - category: Filter spesifik kategori (opsional).
    - min_price / max_price: Filter rentang harga (opsional).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    q = query.strip().lower() if query else ""
    cat_filter = category.strip().lower() if category and category.strip().lower() != "all" else None
    dec_min = Decimal(str(min_price)) if min_price is not None else None
    dec_max = Decimal(str(max_price)) if max_price is not None else None

    results: List[Dict[str, Any]] = []

    for m in markets:
        market_id = _get_attr_or_key(m, "market_id", "")
        market_name = _get_attr_or_key(
            m, "market_name", _get_attr_or_key(m, "question", _get_attr_or_key(m, "title", ""))
        )
        cat = _get_attr_or_key(m, "category", "Weather")
        raw_status = _get_attr_or_key(m, "status", "open")

        # 1. Pencarian keyword (case-insensitive partial match pada nama atau kategori)
        name_lower = str(market_name).lower()
        cat_lower = str(cat).lower()
        if q and (q not in name_lower and q not in cat_lower):
            continue

        # 2. Filter kategori opsional
        if cat_filter and cat_filter not in cat_lower:
            continue

        # 3. Validasi harga
        price_yes = _to_decimal(_get_attr_or_key(m, "price_yes"))
        price_no = _to_decimal(_get_attr_or_key(m, "price_no"))
        curr_price = _to_decimal(_get_attr_or_key(m, "current_price"))

        if curr_price is None and price_yes is not None:
            curr_price = price_yes

        # Filter price range opsional jika disediakan
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

        # 4. Waktu resolusi
        raw_res_time = _get_attr_or_key(m, "resolution_time")
        if raw_res_time is None:
            raw_res_time = _get_attr_or_key(m, "end_date")

        res_time = _parse_datetime(raw_res_time)
        res_time_iso = res_time.isoformat() if res_time is not None else None
        time_remaining_str = "-"
        if res_time:
            if res_time.tzinfo is None:
                res_time = res_time.replace(tzinfo=timezone.utc)
            delta_sec = (res_time - now).total_seconds()
            if delta_sec > 0:
                time_remaining_str = format_time_remaining(delta_sec)
            else:
                time_remaining_str = "Passed"

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
            "status": str(raw_status),
            "side": str(side),
        })

    return results

