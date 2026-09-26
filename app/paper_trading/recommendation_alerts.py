"""
Notifikasi Telegram untuk rekomendasi market suhu (jendela menjelang jam puncak lokal kota).

Dijalankan setiap siklus Market Collector: event yang baru masuk jendela rekomendasi dikirim
sebagai satu pesan daftar, lalu dicatat di tabel recommendation_alerts agar tidak terkirim dua
kali (juga setelah restart). Format per event:

    BUY #Paris in odd 56.8¢ peak hour akan terjadi di jam 21:15–22:15 WIB.
       28°C · suhu tertinggi · 16:15–17:15 waktu lokal
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.models import RecommendationAlert

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


def format_recommendation(event: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """Dua baris per event: kalimat BUY (jam dalam zona NOTIFY_TIMEZONE) + detail bracket."""
    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    label = settings.NOTIFY_TIMEZONE_LABEL
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)

    top = event["markets"][0]  # bracket dengan peluang YES tertinggi
    peak_start_city = datetime.fromisoformat(event["peak_start"])
    peak_end_city = peak_start_city + timedelta(hours=settings.TEMP_PEAK_DURATION_HOURS)
    peak_start, peak_end = peak_start_city.astimezone(tz), peak_end_city.astimezone(tz)
    day_note = "" if peak_start.date() == now_local.date() else f" ({peak_start:%d %b})"

    return (
        f"BUY {city_hashtag(event['city'])} in odd {_format_odd(top.get('price_yes'))} "
        f"peak hour akan terjadi di jam {peak_start:%H:%M}–{peak_end:%H:%M} {label}{day_note}.\n"
        f"   {top.get('bracket')} · {KIND_LABELS.get(event['kind'], event['kind'])} · "
        f"{peak_start_city:%H:%M}–{peak_end_city:%H:%M} waktu lokal"
    )


def build_recommendation_message(events: List[Dict[str, Any]], title: str = "📋 Rekomendasi Paper Trading",
                                 now: Optional[datetime] = None) -> str:
    lines = [title, ""]
    for event in events:
        lines.append(format_recommendation(event, now=now))
    lines.append("")
    lines.append("Bracket = peluang YES tertinggi saat ini. Paper trading, bukan saran finansial.")
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

    events = [e for e in get_market_suggestions(now=now) if e.get("markets")]
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
