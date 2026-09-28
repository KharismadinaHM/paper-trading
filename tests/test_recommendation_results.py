"""
Test pelacakan hasil saran beli bot (WIN / LOSS / bracket pemenang) dan statistik win rate.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from app.core.database import get_db_session
from app.paper_trading.models import (
    MarketLatest, MarketResolution, RecommendationAlert, RecommendationAlertMarket,
)
from app.paper_trading.recommendation_results import get_recommendation_stats, track_recommendation_results
from app.paper_trading.telegram_bot import handle_incoming_message

UTC = timezone.utc
SENT = datetime(2026, 9, 26, 4, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def add_alert(city="Hong Kong", kind="highest", date="2026-09-26", price="0.5", brackets=("31°C", "30°C", "32°C"),
              sent_at=SENT, with_markets=True):
    key = f"{city}|{kind}|{date}"
    db = get_db_session()
    try:
        db.add(RecommendationAlert(event_key=key, city=city, kind=kind, local_date=date, market_id=f"{key}#0",
                                   price_yes=Decimal(price), sent_at=sent_at, bracket=brackets[0]))
        db.flush()
        if with_markets:
            for rank, b in enumerate(brackets):
                db.add(RecommendationAlertMarket(event_key=key, market_id=f"{key}#{rank}", bracket=b, rank=rank))
        db.commit()
    finally:
        db.close()
    return key


def resolver(winners):
    """Pengganti sync_markets_by_condition_ids: market di `winners` resolve YES, sisanya NO."""
    def fake_sync(ids):
        db = get_db_session()
        try:
            for market_id in ids:
                if db.get(MarketResolution, market_id) is None:
                    db.add(MarketResolution(market_id=market_id, winning_outcome="YES" if market_id in winners else "NO"))
            db.commit()
        finally:
            db.close()
        return {}
    return patch("app.market_collector.collector.sync_markets_by_condition_ids", side_effect=fake_sync)


def get_alert(key):
    db = get_db_session()
    try:
        return db.get(RecommendationAlert, key)
    finally:
        db.close()


class TestTracking:

    def test_win_when_suggested_bracket_resolves_yes(self):
        key = add_alert()
        with resolver({f"{key}#0"}):
            assert track_recommendation_results(now=LATER) == {"checked": 1, "resolved": 1}
        alert = get_alert(key)
        assert (alert.result, alert.winning_bracket) == ("WIN", "31°C") and alert.resolved_at is not None

    def test_loss_records_actual_winning_bracket(self):
        key = add_alert()
        with resolver({f"{key}#2"}):
            track_recommendation_results(now=LATER)
        alert = get_alert(key)
        assert (alert.result, alert.winning_bracket) == ("LOSS", "32°C")

    def test_not_checked_before_market_date_has_passed(self):
        add_alert(date="2026-09-27")
        with resolver(set()) as fake:
            assert track_recommendation_results(now=LATER)["checked"] == 0
        fake.assert_not_called()

    def test_unresolved_market_retried_after_check_interval(self):
        key = add_alert()
        with patch("app.market_collector.collector.sync_markets_by_condition_ids", return_value={}) as fake:
            track_recommendation_results(now=LATER)
            track_recommendation_results(now=LATER + timedelta(minutes=5))   # masih dalam jeda
            track_recommendation_results(now=LATER + timedelta(minutes=31))
        assert fake.call_count == 2 and get_alert(key).result is None

    def test_old_alert_without_brackets_is_backfilled_from_market_latest(self):
        key = add_alert(with_markets=False)
        db = get_db_session()
        try:
            for market_id, temp in ((f"{key}#0", "31°C"), ("0xother", "33°C"), ("0xla", None)):
                city = "Los Angeles" if temp is None else "Hong Kong"
                name = f"Will the highest temperature in {city} be {temp or '80°F'} on September 26?"
                db.add(MarketLatest(market_id=market_id, market_name=name, status="closed", timestamp=SENT,
                                    end_date=datetime(2026, 9, 26, 12, tzinfo=UTC)))
            db.commit()
        finally:
            db.close()
        with resolver({"0xother"}):
            track_recommendation_results(now=LATER)
        alert = get_alert(key)
        assert (alert.result, alert.winning_bracket) == ("LOSS", "33°C")


class TestStats:

    def _seed(self):
        for city, price, winner in (("Hong Kong", "0.5", 0), ("Tokyo", "0.25", 0), ("Madrid", "0.6", 1),
                                    ("Paris", "0.4", 2)):
            key = add_alert(city=city, price=price)
            with resolver({f"{key}#{winner}"}):
                track_recommendation_results(now=LATER)
        add_alert(city="Oslo", date="2026-09-27")  # belum ada hasil

    def test_win_rate_odds_and_roi(self):
        self._seed()
        s = get_recommendation_stats(now=LATER)
        assert (s["sent"], s["decided"], s["wins"], s["losses"], s["pending"]) == (5, 4, 2, 2, 1)
        assert s["win_rate"] == 0.5
        assert round(s["avg_odds"], 4) == 0.4375
        # ROI per $1: HK +1.0, Tokyo +3.0, Madrid −1, Paris −1 → rata-rata +0.5
        assert s["roi"] == 0.5
        assert s["winner_in_alternatives"] == 2
        assert s["by_kind"]["highest"]["decided"] == 4 and s["by_kind"]["lowest"]["decided"] == 0

    def test_days_filter(self):
        self._seed()
        add_alert(city="Lima", sent_at=SENT - timedelta(days=20))
        assert get_recommendation_stats(now=LATER)["sent"] == 6
        assert get_recommendation_stats(days=7, now=LATER)["sent"] == 5

    def test_telegram_statistik_command(self):
        self._seed()
        with patch("app.paper_trading.recommendation_results.datetime") as fake_dt:
            fake_dt.now.return_value = LATER
            reply = handle_incoming_message("/statistik", sender_chat_id="1", allowed_chat_id="1")
        assert "Win rate 50%" in reply
        assert "ROI per $1: +50%" in reply
        assert "❌ #Madrid max 09-26 · 31°C @ 60¢ → menang: 30°C" in reply

    def test_statistik_without_data(self):
        reply = handle_incoming_message("/statistik", sender_chat_id="1", allowed_chat_id="1")
        assert "Belum ada saran yang terkirim" in reply

    def test_stats_api(self):
        from fastapi.testclient import TestClient
        from app.dashboard import app
        self._seed()
        data = TestClient(app).get("/api/recommendations/stats").json()
        assert data["wins"] == 2 and data["recent"][0]["result"] in ("WIN", "LOSS")
        assert 'id="recStatsContainer"' in TestClient(app).get("/").text


def test_sending_alert_records_all_brackets():
    from app.paper_trading.recommendation_alerts import send_new_recommendation_alerts
    ev = {
        "event_key": "Hong Kong|highest|2026-09-26", "city": "Hong Kong", "kind": "highest",
        "local_date": "2026-09-26", "peak_start": "2026-09-26T14:00:00+08:00",
        "markets": [{"market_id": "0xa", "bracket": "31°C", "price_yes": 0.5},
                    {"market_id": "0xb", "bracket": "30°C", "price_yes": 0.3}],
    }
    with patch("app.paper_service.get_market_suggestions", return_value=[ev]), \
         patch("app.paper_trading.telegram.send_telegram_message", return_value={"success": True}):
        send_new_recommendation_alerts(now=SENT)
    db = get_db_session()
    try:
        rows = db.query(RecommendationAlertMarket).order_by(RecommendationAlertMarket.rank).all()
        assert [(r.market_id, r.bracket, r.rank) for r in rows] == [("0xa", "31°C", 0), ("0xb", "30°C", 1)]
        assert db.get(RecommendationAlert, ev["event_key"]).bracket == "31°C"
    finally:
        db.close()
