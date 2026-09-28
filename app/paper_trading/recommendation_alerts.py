"""
Notifikasi Telegram untuk rekomendasi market suhu (jendela menjelang jam puncak lokal kota).

Dijalankan setiap siklus Market Collector: event yang baru masuk jendela rekomendasi dikirim
sebagai satu pesan daftar, lalu dicatat di tabel recommendation_alerts agar tidak terkirim dua
kali (juga setelah restart). Hanya kota dengan total volume market suhu terbesar
(TELEGRAM_RECOMMENDATION_TOP_CITIES, default 7) yang dikirim. Format per event:

    BUY #Paris di suhu 28°C (YES) in odd 56.8¢ peak hour akan terjadi di jam 21:15–22:15 WIB.
       Suhu tertinggi · puncak 16:15–17:15 waktu lokal · Vol $12K
       Alternatif: 27°C (22¢), 29°C (15¢)
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.models import RecommendationAlert, RecommendationAlertMarket

logger = get_logger("recommendation_alerts")

KIND_LABELS = {"highest": "suhu tertinggi", "lowest": "suhu terendah"}


def city_hashtag(city: str) -> str:
    """'Seoul (Incheon)' → '#SeoulIncheon', 'New York City' → '#NewYorkCity'."""
    return "#" + "".join(part[:1].upper() + part[1:] for part in re.findall(r"[A-Za-z0-9]+", city))


def _format_odd(price: Optional[float]) -> str:
    if price is None:
        return "-"
    cents = round(float(price) * 100, 1)  # 0.57 * 100 = 56.999… → 57.0
    return f"{cents:.0f}¢" if cents.is_integer() else f"{cents:.1f}¢"


def _format_volume(volume: Optional[float]) -> str:
    volume = float(volume or 0)
    if volume >= 1_000_000:
        return f"${volume / 1_000_000:.1f}M"
    if volume >= 1_000:
        return f"${volume / 1_000:.0f}K"
    return f"${volume:.0f}"


def top_volume_events(events: List[Dict[str, Any]], now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """
    Saring event ke kota-kota dengan total volume market suhu terbesar (seluruh market suhu open,
    bukan hanya yang sedang di jendela), sehingga daftar kotanya stabil sepanjang hari.
    Jika data volume belum tersedia, event tidak disaring.
    """
    limit = settings.TELEGRAM_RECOMMENDATION_TOP_CITIES
    if limit <= 0 or not events:
        return events
    from app.paper_service import get_top_volume_cities

    top = get_top_volume_cities(limit, now=now)
    if top is None:
        return events
    allowed = set(top)
    return [e for e in events if e["city"] in allowed]


def format_recommendation(event: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """
    Per event: kalimat BUY berisi saran suhu (bracket peluang YES tertinggi) dan jam puncak dalam
    zona NOTIFY_TIMEZONE, lalu detail jenis/jam lokal, alternatif bracket, dan peringatan jika
    harga beberapa bracket sama persis (tanda market sepi).
    """
    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    label = settings.NOTIFY_TIMEZONE_LABEL
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)

    markets = event["markets"]
    top = markets[0]  # bracket dengan peluang YES tertinggi
    yes_label = top.get("outcome_yes_label") or "YES"
    peak_start_city = datetime.fromisoformat(event["peak_start"])
    peak_end_city = peak_start_city + timedelta(hours=settings.TEMP_PEAK_DURATION_HOURS)
    peak_start, peak_end = peak_start_city.astimezone(tz), peak_end_city.astimezone(tz)
    day_note = "" if peak_start.date() == now_local.date() else f" ({peak_start:%d %b})"

    lines = [
        f"BUY {city_hashtag(event['city'])} di suhu {top.get('bracket')} ({yes_label.upper()}) "
        f"in odd {_format_odd(top.get('price_yes'))} "
        f"peak hour akan terjadi di jam {peak_start:%H:%M}–{peak_end:%H:%M} {label}{day_note}.",
        f"   {KIND_LABELS.get(event['kind'], event['kind']).capitalize()} · "
        f"puncak {peak_start_city:%H:%M}–{peak_end_city:%H:%M} waktu lokal"
        + (f" · Vol {_format_volume(event['volume'])}" if event.get("volume") else ""),
    ]
    alternatives = [m for m in markets[1:3] if m.get("price_yes") is not None]
    if alternatives:
        lines.append("   Alternatif: " + ", ".join(
            f"{m.get('bracket')} ({_format_odd(m.get('price_yes'))})" for m in alternatives))
    ties = sum(1 for m in markets if m.get("price_yes") == top.get("price_yes"))
    if ties > 1:
        lines.append(f"   ⚠️ {ties} bracket berharga sama ({_format_odd(top.get('price_yes'))}) — market sepi, "
                     "saran suhu kurang dapat diandalkan")
    return "\n".join(lines)


def build_recommendation_message(events: List[Dict[str, Any]], title: str = "📋 Rekomendasi Paper Trading",
                                 now: Optional[datetime] = None) -> str:
    lines = [title, ""]
    for event in events:
        lines.append(format_recommendation(event, now=now))
    lines.append("")
    lines.append("Saran suhu = bracket dengan peluang YES tertinggi saat ini. Paper trading, bukan saran finansial.")
    return "\n".join(lines)


def send_new_recommendation_alerts(now: Optional[datetime] = None) -> List[str]:
    """
    Kirim event rekomendasi yang belum pernah dinotifikasi. Mengembalikan event_key yang terkirim.
    Event hanya dicatat setelah pesan berhasil terkirim, sehingga kegagalan jaringan dicoba lagi
    pada siklus berikutnya selama jendelanya masih berlangsung.
    """
    if not settings.TELEGRAM_RECOMMENDATION_ALERTS:
        return []

    from app.paper_service import get_market_suggestions
    from app.paper_trading.telegram import send_telegram_message

    events = top_volume_events([e for e in get_market_suggestions(now=now) if e.get("markets")], now=now)
    if not events:
        return []

    db = get_db_session()
    try:
        keys = [e["event_key"] for e in events]
        already = {row[0] for row in db.query(RecommendationAlert.event_key).filter(RecommendationAlert.event_key.in_(keys))}
        new_events = [e for e in events if e["event_key"] not in already]
        if not new_events:
            return []

        result = send_telegram_message(build_recommendation_message(new_events, now=now))
        if not result.get("success"):
            logger.warning("Notifikasi rekomendasi tidak terkirim: %s", result.get("error"))
            return []

        sent_at = now or datetime.now(timezone.utc)
        for e in new_events:
            top = e["markets"][0]
            db.add(RecommendationAlert(
                event_key=e["event_key"], city=e["city"], kind=e["kind"], local_date=e["local_date"],
                market_id=top.get("market_id"), price_yes=top.get("price_yes"), sent_at=sent_at,
                bracket=top.get("bracket"),
            ))
            # Semua bracket saat saran dikirim, supaya nanti terlihat bracket mana yang menang
            for rank, m in enumerate(e["markets"]):
                if m.get("market_id"):
                    db.add(RecommendationAlertMarket(
                        event_key=e["event_key"], market_id=m["market_id"], bracket=m.get("bracket"),
                        rank=rank, price_yes=m.get("price_yes"),
                    ))
        db.commit()
        logger.info("Notifikasi rekomendasi terkirim: %s", [e["event_key"] for e in new_events])
        return [e["event_key"] for e in new_events]
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def run_recommendation_alerts() -> int:
    """Dipanggil dari loop collector; tidak pernah melempar exception."""
    try:
        return len(send_new_recommendation_alerts())
    except Exception as err:
        logger.error("Gagal memproses notifikasi rekomendasi: %s", err, exc_info=True)
        return 0
