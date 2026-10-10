"""
Pelacakan hasil saran beli bot (notifikasi rekomendasi Telegram) dan statistik win rate-nya.

Setiap saran tercatat di recommendation_alerts (bracket utama + odds saat dikirim) dan
recommendation_alert_markets (semua bracket event itu). Setelah tanggal market lewat, tracker
mengambil status resolusi dari Gamma API dan mengisi:
- result: WIN jika bracket saran resolve YES, LOSS jika NO, VOID jika dibatalkan;
- winning_bracket: bracket yang ternyata menang (bisa berbeda dari saran).

ROI dihitung seolah membeli $1 YES di odds saat saran dikirim:
menang → +(1 − p) / p, kalah → −1.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.models import MarketResolution, RecommendationAlert, RecommendationAlertMarket

logger = get_logger("recommendation_results")

RESULT_BY_OUTCOME = {"YES": "WIN", "NO": "LOSS", "INVALID": "VOID"}
CHECK_INTERVAL = timedelta(minutes=30)  # jeda cek ulang ke Gamma per saran yang belum ada hasil
GIVE_UP_AFTER = timedelta(days=10)      # berhenti melacak jika market tak kunjung resolve


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _backfill_alert_markets(db, alert: RecommendationAlert, now: datetime) -> List[RecommendationAlertMarket]:
    """
    Saran yang dikirim sebelum bracket ikut dicatat: susun ulang bracket event dari market_latest
    (kota + jenis + tanggal yang sama). Bracket saran tetap rank 0.
    """
    from app.paper_service import get_market_snapshots
    from app.paper_trading.cities import resolve_city
    from app.paper_trading.weather_peaks import _as_datetime, bracket_label, parse_temperature_market

    target_date = date.fromisoformat(alert.local_date)
    rows: List[RecommendationAlertMarket] = []
    others = []
    for m in get_market_snapshots(now=now, include_resolved=True, db=db):
        parsed = parse_temperature_market(m["market_name"], _as_datetime(m.get("end_date")), now)
        if (parsed is None or parsed.kind != alert.kind or parsed.local_date != target_date
                or resolve_city(parsed.city) != alert.city):
            continue
        if m["market_id"] == alert.market_id:
            if alert.bracket is None:
                alert.bracket = bracket_label(m["market_name"])
            continue
        others.append((m["market_id"], bracket_label(m["market_name"])))
    if alert.market_id:
        rows.append(RecommendationAlertMarket(event_key=alert.event_key, market_id=alert.market_id,
                                              bracket=alert.bracket, rank=0, price_yes=alert.price_yes))
    # Harga bracket lain saat saran dikirim tidak diketahui untuk data lama
    for i, (market_id, bracket) in enumerate(sorted(others, key=lambda x: x[1] or ""), start=1):
        rows.append(RecommendationAlertMarket(event_key=alert.event_key, market_id=market_id,
                                              bracket=bracket, rank=i))
    db.add_all(rows)
    return rows


def _pending_alerts(db, now: datetime) -> List[RecommendationAlert]:
    """Saran yang tanggal market-nya sudah lewat (UTC) dan belum selesai dilacak."""
    today = now.date().isoformat()
    pending = []
    for alert in db.query(RecommendationAlert).filter(RecommendationAlert.resolved_at.is_(None)):
        if alert.local_date >= today:
            continue
        checked = _aware(alert.checked_at)
        if checked is not None and now - checked < CHECK_INTERVAL:
            continue
        pending.append(alert)
    return pending


def track_recommendation_results(now: Optional[datetime] = None) -> Dict[str, int]:
    """Satu siklus: ambil resolusi market dari saran yang tertunda, lalu isi hasilnya."""
    from app.market_collector.collector import sync_markets_by_condition_ids

    now = now or datetime.now(timezone.utc)
    summary = {"checked": 0, "resolved": 0}
    db = get_db_session()
    try:
        alerts = _pending_alerts(db, now)
        if not alerts:
            return summary
        markets: Dict[str, List[RecommendationAlertMarket]] = {}
        for alert in alerts:
            rows = db.query(RecommendationAlertMarket).filter_by(event_key=alert.event_key).all()
            markets[alert.event_key] = rows or _backfill_alert_markets(db, alert, now)
        db.commit()

        ids = [r.market_id for rows in markets.values() for r in rows if r.winning_outcome is None]
        if ids:
            sync_markets_by_condition_ids(ids)  # mencatat market_resolutions untuk market yang sudah resolve
        outcomes = {
            r.market_id: r.winning_outcome
            for r in db.query(MarketResolution).filter(MarketResolution.market_id.in_(ids))
        } if ids else {}

        for alert in alerts:
            summary["checked"] += 1
            alert.checked_at = now
            rows = markets[alert.event_key]
            for r in rows:
                r.winning_outcome = r.winning_outcome or outcomes.get(r.market_id)
            top = next((r for r in rows if r.market_id == alert.market_id), None)
            if top is not None and top.winning_outcome:
                alert.result = RESULT_BY_OUTCOME.get(top.winning_outcome)
            winner = next((r for r in rows if r.winning_outcome == "YES"), None)
            if winner is not None:
                alert.winning_bracket = winner.bracket
            all_done = bool(rows) and all(r.winning_outcome for r in rows)
            if alert.result and (winner is not None or all_done):
                alert.resolved_at = now
                summary["resolved"] += 1
            elif now - _aware(alert.sent_at) > GIVE_UP_AFTER:
                alert.resolved_at = now  # result tetap kosong jika saran utama tak pernah resolve
        db.commit()
        if summary["resolved"]:
            logger.info("Hasil saran bot terisi: %s", summary)
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def run_recommendation_tracking() -> int:
    """Dipanggil dari loop collector; tidak pernah melempar exception."""
    try:
        return track_recommendation_results()["resolved"]
    except Exception as err:
        logger.error("Gagal melacak hasil saran bot: %s", err, exc_info=True)
        return 0


def _roi(p: Optional[float], result: str) -> Optional[float]:
    if p is None or p <= 0:
        return None
    return (1 - p) / p if result == "WIN" else -1.0


def _summarize(rows: List[RecommendationAlert]) -> Dict[str, Any]:
    decided = [a for a in rows if a.result in ("WIN", "LOSS")]
    wins = sum(1 for a in decided if a.result == "WIN")
    prices = [float(a.price_yes) for a in decided if a.price_yes is not None]
    rois = [x for x in (_roi(float(a.price_yes) if a.price_yes is not None else None, a.result) for a in decided)
            if x is not None]
    return {
        "sent": len(rows),
        "decided": len(decided),
        "wins": wins,
        "losses": len(decided) - wins,
        "void": sum(1 for a in rows if a.result == "VOID"),
        "pending": sum(1 for a in rows if a.result is None and a.resolved_at is None),
        "win_rate": wins / len(decided) if decided else None,
        "avg_odds": sum(prices) / len(prices) if prices else None,
        "roi": sum(rois) / len(rois) if rois else None,
    }


def get_recommendation_stats(days: Optional[int] = None, now: Optional[datetime] = None,
                             recent_limit: int = 10) -> Dict[str, Any]:
    """
    Statistik saran bot. `days` membatasi ke saran yang dikirim dalam N hari terakhir.
    `winner_in_alternatives` = saran kalah tetapi pemenangnya bracket alternatif #2/#3.
    """
    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    try:
        query = db.query(RecommendationAlert)
        if days:
            query = query.filter(RecommendationAlert.sent_at >= now - timedelta(days=days))
        alerts = query.order_by(RecommendationAlert.sent_at.desc()).all()

        alt_ranks = {
            key: rank for key, rank in db.query(RecommendationAlertMarket.event_key, RecommendationAlertMarket.rank)
            .filter(RecommendationAlertMarket.winning_outcome == "YES")
        }
        losses = [a for a in alerts if a.result == "LOSS"]
        stats = _summarize(alerts)
        stats.update({
            "days": days,
            "by_kind": {kind: _summarize([a for a in alerts if a.kind == kind]) for kind in ("highest", "lowest")},
            "winner_in_alternatives": sum(1 for a in losses if alt_ranks.get(a.event_key) in (1, 2)),
            "recent": [
                {
                    "city": a.city, "kind": a.kind, "local_date": a.local_date, "bracket": a.bracket,
                    "price_yes": float(a.price_yes) if a.price_yes is not None else None,
                    "result": a.result, "winning_bracket": a.winning_bracket,
                }
                for a in alerts if a.result in ("WIN", "LOSS", "VOID")
            ][:recent_limit],
        })
        return stats
    finally:
        db.close()
