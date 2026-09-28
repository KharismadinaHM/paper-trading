"""
Test data live rekomendasi: stasiun resolusi & token dari Gamma, likuiditas order book, observasi
stasiun, dan tampilannya di notifikasi Telegram.
"""
from datetime import date, datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.core.database import get_db_session
from app.market_collector.collector import parse_market_dict, parse_resolution_station
from app.paper_trading import live_market_data as live
from app.paper_trading.models import RecommendationAlert
from app.paper_trading.recommendation_alerts import format_recommendation, send_new_recommendation_alerts
from app.paper_trading.telegram_bot import handle_incoming_message

UTC = timezone.utc
NOW = datetime(2026, 9, 26, 4, 30, tzinfo=UTC)
HKT = ZoneInfo("Asia/Hong_Kong")


@pytest.fixture(autouse=True)
def fresh_cache():
    live.clear_cache()
    yield
    live.clear_cache()


def event(markets=None, **extra):
    ev = {
        "event_key": "Hong Kong|highest|2026-09-26", "city": "Hong Kong", "kind": "highest",
        "local_date": "2026-09-26", "peak_start": "2026-09-26T14:00:00+08:00",
        "markets": markets or [
            {"market_id": "0xa", "bracket": "31°C", "price_yes": 0.5, "yes_token_id": "ta"},
            {"market_id": "0xb", "bracket": "30°C", "price_yes": 0.3, "yes_token_id": "tb"},
            {"market_id": "0xc", "bracket": "32°C", "price_yes": 0.1, "yes_token_id": "tc"},
        ],
    }
    ev.update(extra)
    return ev


class TestGammaParsing:

    @pytest.mark.parametrize("text,station", [
        ("… available here: https://www.weather.gov/wrh/timeseries?site=rksi", "RKSI"),
        ("https://www.wunderground.com/history/daily/tw/taipei/RCSS.", "RCSS"),
        ("recorded by the Hong Kong Observatory in degrees Celsius", "HKO"),
        ("no source", None),
    ])
    def test_resolution_station(self, text, station):
        assert parse_resolution_station({"description": text}) == station

    def test_yes_token_follows_outcome_order(self):
        m = parse_market_dict({
            "conditionId": "0x1", "question": "Q?", "outcomes": '["No","Yes"]', "outcomePrices": '["0.6","0.4"]',
            "clobTokenIds": '["tok-no","tok-yes"]', "resolutionSource": "https://www.weather.gov/wrh/timeseries?site=eglc",
        })
        assert (m["yes_token_id"], m["resolution_station"]) == ("tok-yes", "EGLC")


class TestLiquidity:

    def test_top_suggestion_is_most_likely_liquid_bracket(self):
        ev = event()
        books = {"ta": {"ask": 0.97, "bid": 0.03, "ask_size": 10}, "tb": {"ask": 0.32, "bid": 0.29, "ask_size": 50},
                 "tc": {"ask": 0.12, "bid": 0.09, "ask_size": 5}}
        live.apply_liquidity(ev, books)
        assert ev["liquid"] is True
        assert [m["bracket"] for m in ev["markets"]] == ["30°C", "31°C", "32°C"]
        assert ev["markets"][0]["spread"] == 0.03 and ev["markets"][1]["liquid"] is False
        assert live.entry_price(ev["markets"][0]) == 0.32

    def test_no_liquid_bracket(self):
        ev = event()
        live.apply_liquidity(ev, {t: {"ask": 0.97, "bid": 0.03, "ask_size": 1} for t in ("ta", "tb", "tc")})
        assert ev["liquid"] is False and ev["markets"][0]["bracket"] == "31°C"

    def test_without_book_data_nothing_changes(self):
        ev = event()
        live.apply_liquidity(ev, {})
        assert ev["liquid"] is None and [m["bracket"] for m in ev["markets"]] == ["31°C", "30°C", "32°C"]
        assert live.entry_price(ev["markets"][0]) == 0.5

    def test_fetch_failure_returns_empty(self):
        with patch.object(live, "_http", side_effect=OSError("down")):
            assert live.fetch_order_books(["ta"]) == {}


class TestObservations:

    def test_extreme_since_local_midnight_with_fahrenheit(self):
        rows = [(datetime(2026, 9, 25, 15, 0, tzinfo=UTC), 30.0),   # 23:00 HKT hari sebelumnya → diabaikan
                (datetime(2026, 9, 26, 5, 0, tzinfo=UTC), 22.2),
                (datetime(2026, 9, 26, 6, 0, tzinfo=UTC), 23.0),
                (datetime(2026, 9, 26, 7, 0, tzinfo=UTC), 21.0)]
        obs = live.observed_extreme("highest", "F", HKT, date(2026, 9, 26), rows)
        assert obs["value"] == 73.4 and obs["current"] == 69.8 and obs["at"].hour == 14
        assert live.observed_extreme("lowest", "C", HKT, date(2026, 9, 26), rows)["value"] == 21.0

    def test_apply_observations_uses_event_station(self):
        ev = event(station="VHHH", city="Hong Kong")
        rows = {"VHHH": [(datetime(2026, 9, 26, 5, 0, tzinfo=UTC), 31.0), (datetime(2026, 9, 26, 6, 0, tzinfo=UTC), 30.0)]}
        with patch.object(live, "fetch_metar_observations", return_value=rows):
            live.apply_observations([ev])
        assert ev["observation"]["value"] == 31.0 and ev["observation"]["station"] == "VHHH"


class TestTelegram:

    def _enriched(self):
        ev = event()
        live.apply_liquidity(ev, {"ta": {"ask": 0.52, "bid": 0.49, "ask_size": 20}, "tb": {"ask": 0.33, "bid": 0.3},
                                  "tc": {"ask": 0.11, "bid": 0.09}})
        ev["observation"] = {"station": "RKSI", "value": 31.0, "unit": "C",
                             "at": datetime(2026, 9, 26, 12, 20, tzinfo=HKT),
                             "current": 30.0, "current_at": datetime(2026, 9, 26, 13, 0, tzinfo=HKT)}
        return ev

    def test_message_uses_ask_and_shows_book_and_observation(self):
        text = format_recommendation(self._enriched(), now=NOW)
        assert "di suhu 31°C (YES) in odd 52¢ peak hour" in text
        assert "Order book: bid 49¢ / ask 52¢ (spread 3¢)" in text
        assert "Terukur di RKSI: max 31°C (12:20) · terakhir 30°C (13:00) waktu lokal" in text
        assert "Alternatif: 30°C (33¢), 32°C (11¢)" in text

    def test_illiquid_event_not_sent_and_ask_is_recorded(self):
        liquid, illiquid = self._enriched(), event(event_key="Tokyo|highest|2026-09-26", city="Tokyo", liquid=False)
        sent = []
        with patch("app.paper_service.get_market_suggestions", return_value=[liquid, illiquid]), \
             patch("app.paper_trading.telegram.send_telegram_message",
                   side_effect=lambda text, **kw: sent.append(text) or {"success": True}):
            assert send_new_recommendation_alerts(now=NOW) == ["Hong Kong|highest|2026-09-26"]
        assert "#Tokyo" not in sent[0]
        db = get_db_session()
        try:
            assert float(db.get(RecommendationAlert, "Hong Kong|highest|2026-09-26").price_yes) == 0.52
        finally:
            db.close()

    def test_illiquid_warning_in_list(self):
        assert "⚠️ Tidak ada bracket likuid" in format_recommendation(event(liquid=False), now=NOW)


def test_stats_command_renamed():
    help_text = handle_incoming_message("/help", sender_chat_id="1", allowed_chat_id="1")
    assert "/stats" in help_text and "/statistik" not in help_text
    assert "Statistik Saran Bot" in handle_incoming_message("/stats", sender_chat_id="1", allowed_chat_id="1")
