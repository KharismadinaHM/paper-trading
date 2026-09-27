"""
Test notifikasi Telegram rekomendasi jam puncak, perintah bot /rekomendasi, dan posisi section
rekomendasi di dashboard.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.core.database import get_db_session
from app.paper_trading.models import RecommendationAlert
from app.paper_trading.recommendation_alerts import (
    build_recommendation_message,
    city_hashtag,
    format_recommendation,
    send_new_recommendation_alerts,
)
from app.paper_trading.telegram_bot import handle_incoming_message

UTC = timezone.utc
NOW = datetime(2026, 9, 26, 4, 30, tzinfo=UTC)  # 11:30 WIB


def event(city="Hong Kong", kind="highest", peak_start="2026-09-26T14:00:00+08:00", price=0.568,
          bracket="31°C or higher", date="2026-09-26"):
    return {
        "event_key": f"{city}|{kind}|{date}", "city": city, "kind": kind, "local_date": date,
        "peak_start": peak_start, "window_label": "12:00–13:00 HKT · puncak 14:00–15:00",
        "markets": [
            {"market_id": f"0x{city[:3]}{kind[:1]}", "bracket": bracket, "price_yes": price, "price_no": 1 - price},
            {"market_id": "0xother", "bracket": "30°C", "price_yes": 0.2, "price_no": 0.8},
        ],
    }


class TestFormatting:

    @pytest.mark.parametrize("city,tag", [
        ("Hong Kong", "#HongKong"), ("Seoul (Incheon)", "#SeoulIncheon"),
        ("New York City", "#NewYorkCity"), ("Sao Paulo", "#SaoPaulo"),
    ])
    def test_city_hashtag(self, city, tag):
        assert city_hashtag(city) == tag

    def test_buy_line_includes_suggested_temperature_and_wib_time(self):
        lines = format_recommendation(event(), now=NOW).split("\n")
        # 14:00–15:00 HKT (UTC+8) = 13:00–14:00 WIB (UTC+7)
        assert lines[0] == ("BUY #HongKong di suhu 31°C or higher (YES) in odd 56.8¢ "
                            "peak hour akan terjadi di jam 13:00–14:00 WIB.")
        assert lines[1].strip() == "Suhu tertinggi · puncak 14:00–15:00 waktu lokal"
        assert lines[2].strip() == "Alternatif: 30°C (20¢)"
        assert len(lines) == 3  # tidak ada peringatan harga sama

    def test_up_down_style_outcome_label_is_used(self):
        ev = event()
        ev["markets"][0]["outcome_yes_label"] = "Up"
        assert "(UP) in odd" in format_recommendation(ev, now=NOW)

    def test_warning_when_several_brackets_share_the_top_price(self):
        ev = event(price=0.5)
        ev["markets"] = [{"market_id": f"0x{i}", "bracket": f"{9 + i}°C", "price_yes": 0.5} for i in range(3)]
        text = format_recommendation(ev, now=NOW)
        assert "Alternatif: 10°C (50¢), 11°C (50¢)" in text
        assert "⚠️ 3 bracket berharga sama (50¢)" in text

    def test_date_shown_when_peak_falls_on_another_wib_day(self):
        # Los Angeles 15:00 PDT (UTC-7) = 05:00 WIB keesokan harinya
        text = format_recommendation(event("Los Angeles", peak_start="2026-09-26T15:00:00-07:00"), now=NOW)
        assert "di jam 05:00–06:00 WIB (27 Sep)." in text

    def test_volume_shown_on_detail_line(self):
        ev = {**event(), "volume": 12_345}
        assert format_recommendation(ev, now=NOW).split("\n")[1].strip() == (
            "Suhu tertinggi · puncak 14:00–15:00 waktu lokal · Vol $12K")

    def test_whole_cent_odds_without_decimal(self):
        assert "in odd 57¢" in format_recommendation(event(price=0.57), now=NOW)

    def test_list_message_contains_every_event(self):
        msg = build_recommendation_message([event(), event("Madrid", peak_start="2026-09-26T16:15:00+02:00")], now=NOW)
        assert msg.startswith("📋 Rekomendasi Paper Trading")
        assert msg.count("BUY #") == 2 and "#Madrid" in msg


class TestSending:

    def _alerts(self):
        db = get_db_session()
        try:
            return {r.event_key for r in db.query(RecommendationAlert)}
        finally:
            db.close()

    def test_new_events_sent_once_as_single_list(self):
        sent = []
        with patch("app.paper_service.get_market_suggestions", return_value=[event(), event("Madrid")]), \
             patch("app.paper_trading.telegram.send_telegram_message",
                   side_effect=lambda text, **kw: sent.append(text) or {"success": True}):
            assert len(send_new_recommendation_alerts(now=NOW)) == 2
            assert send_new_recommendation_alerts(now=NOW) == []  # siklus berikutnya: tidak dobel
        assert len(sent) == 1 and sent[0].count("BUY #") == 2
        assert self._alerts() == {"Hong Kong|highest|2026-09-26", "Madrid|highest|2026-09-26"}

    def test_only_newly_entered_events_are_sent(self):
        sent = []
        fake_send = lambda text, **kw: sent.append(text) or {"success": True}  # noqa: E731
        with patch("app.paper_trading.telegram.send_telegram_message", side_effect=fake_send):
            with patch("app.paper_service.get_market_suggestions", return_value=[event()]):
                send_new_recommendation_alerts(now=NOW)
            with patch("app.paper_service.get_market_suggestions", return_value=[event(), event("Tokyo")]):
                send_new_recommendation_alerts(now=NOW)
        assert len(sent) == 2 and "#Tokyo" in sent[1] and "#HongKong" not in sent[1]

    def test_failed_send_is_retried_next_cycle(self):
        with patch("app.paper_service.get_market_suggestions", return_value=[event()]):
            with patch("app.paper_trading.telegram.send_telegram_message", return_value={"success": False, "error": "x"}):
                assert send_new_recommendation_alerts(now=NOW) == []
            assert self._alerts() == set()
            with patch("app.paper_trading.telegram.send_telegram_message", return_value={"success": True}):
                assert send_new_recommendation_alerts(now=NOW) == ["Hong Kong|highest|2026-09-26"]

    def test_only_top_volume_cities_are_sent(self):
        sent = []
        with patch("app.paper_service.get_market_suggestions",
                   return_value=[event(), event("Madrid"), event("Tokyo")]), \
             patch("app.paper_service.get_top_volume_cities", return_value=["Tokyo", "Hong Kong"]), \
             patch("app.paper_trading.telegram.send_telegram_message",
                   side_effect=lambda text, **kw: sent.append(text) or {"success": True}):
            assert send_new_recommendation_alerts(now=NOW) == ["Hong Kong|highest|2026-09-26", "Tokyo|highest|2026-09-26"]
        assert "#Madrid" not in sent[0]
        # Madrid tidak dicatat, sehingga tetap bisa dikirim bila nanti masuk 7 besar
        assert self._alerts() == {"Hong Kong|highest|2026-09-26", "Tokyo|highest|2026-09-26"}

    def test_top_cities_setting_zero_sends_all(self, monkeypatch):
        monkeypatch.setattr(settings, "TELEGRAM_RECOMMENDATION_TOP_CITIES", 0)
        with patch("app.paper_service.get_market_suggestions", return_value=[event(), event("Madrid")]), \
             patch("app.paper_service.get_top_volume_cities", return_value=["Tokyo"]), \
             patch("app.paper_trading.telegram.send_telegram_message", return_value={"success": True}):
            assert len(send_new_recommendation_alerts(now=NOW)) == 2

    def test_disabled_setting_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(settings, "TELEGRAM_RECOMMENDATION_ALERTS", False)
        with patch("app.paper_service.get_market_suggestions", return_value=[event()]), \
             patch("app.paper_trading.telegram.send_telegram_message") as fake_send:
            assert send_new_recommendation_alerts(now=NOW) == []
        fake_send.assert_not_called()


class TestBotCommand:

    def test_rekomendasi_lists_active_events(self):
        with patch("app.paper_service.get_market_suggestions", return_value=[event()]):
            reply = handle_incoming_message("/rekomendasi", sender_chat_id="1", allowed_chat_id="1")
        assert "BUY #HongKong di suhu 31°C or higher (YES) in odd 56.8¢" in reply

    def test_rekomendasi_shows_schedule_when_nothing_active(self):
        schedule = [{"city": "Tokyo", "kind": "highest", "starts_at": "2026-09-26T11:15:00+09:00",
                     "starts_in": "1h 45m"}]
        with patch("app.paper_service.get_market_suggestions", return_value=[]), \
             patch("app.paper_service.get_recommendation_schedule", return_value=schedule):
            reply = handle_incoming_message("/rekomendasi", sender_chat_id="1", allowed_chat_id="1")
        assert "Tokyo (tertinggi) mulai 09:15 WIB" in reply

    def test_rekomendasi_only_lists_top_volume_cities(self):
        with patch("app.paper_service.get_market_suggestions", return_value=[event(), event("Madrid")]), \
             patch("app.paper_service.get_top_volume_cities", return_value=["Madrid"]):
            reply = handle_incoming_message("/rekomendasi", sender_chat_id="1", allowed_chat_id="1")
        assert "#Madrid" in reply and "#HongKong" not in reply

    def test_help_mentions_command(self):
        assert "/rekomendasi" in handle_incoming_message("/help", sender_chat_id="1", allowed_chat_id="1")


def test_suggestions_section_is_between_positions_and_weather():
    from fastapi.testclient import TestClient
    from app.dashboard import app
    html = TestClient(app).get("/").text
    positions = html.index('id="viewPositionsTable"')
    suggestions = html.index('id="suggestedMarketsContainer"')
    weather = html.index('id="weatherEventsContainer"')
    assert positions < suggestions < weather
