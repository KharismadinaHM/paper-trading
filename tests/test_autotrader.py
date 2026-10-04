"""
Test auto paper trader: model BTC, probabilitas bracket cuaca, VWAP order book, risk engine,
eksekusi ke akun paper (satu entri per market), strategi BTC & cuaca, command & API.
"""
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.core.database import get_db_session
from app.market_collector.collector import record_market_observations
from app.paper_service import deposit_paper_funds, get_open_positions
from app.paper_trading import autotrader as at
from app.paper_trading.live_market_data import vwap_for_usd
from app.paper_trading.models import AutotradeDecision
from app.paper_trading.telegram_bot import handle_incoming_message

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def seed_market(market_id="0xbtc", name="Bitcoin Up or Down - test", price="0.50", at=None):
    at = at or NOW
    record_market_observations([{
        "market_id": market_id, "market_name": name, "category": "Crypto", "status": "open", "is_resolved": False,
        "resolution_time": NOW + timedelta(hours=1), "end_date": NOW + timedelta(hours=1),
        "price_yes": Decimal(price), "price_no": Decimal("1") - Decimal(price), "current_price": Decimal(price),
        "outcome_yes_label": "Up", "outcome_no_label": "Down", "timestamp": at,
    }], now=at)


def decision(key="btc|0xbtc", market_id="0xbtc", size=5.0, prob=0.7, price=0.55, fee=0.017):
    return {"key": key, "strategy": "btc", "market_id": market_id, "title": "BTC test", "label": "BTC test · UP",
            "side": "YES", "outcome": "UP", "prob": prob, "price": price, "fee": fee, "edge": prob - price - fee,
            "size": size, "detail": "detail", "url": None}


@pytest.fixture
def funded():
    deposit_paper_funds(Decimal("500"))


@pytest.fixture
def sent():
    messages = []
    with patch("app.paper_trading.telegram.send_telegram_message",
               side_effect=lambda text, **kw: messages.append(text) or {"success": True}):
        yield messages


class TestModels:

    def test_btc_model_probability(self):
        hour = NOW.replace(minute=0, second=0)
        start_ms = int(hour.timestamp() * 1000)
        klines = []
        for i in range(-80, 46):  # 80 menit sebelum open sampai menit ke-45
            ts = start_ms + i * 60_000
            base = 100_000 * (1 + 0.00002 * (i % 2))  # volatilitas sangat kecil
            klines.append([ts, 100_000.0 if i == 0 else base, base])
        klines[-1][2] = 100_300.0  # harga sekarang +0.3% dari open
        m = at.btc_model(klines, hour, hour + timedelta(minutes=45))
        assert m["p_up"] > 0.99 and round(m["change_pct"], 2) == 0.30

    def test_btc_model_needs_opening_candle(self):
        hour = NOW.replace(minute=0, second=0)
        assert at.btc_model([[0, 1.0, 1.0]] * 70, hour, hour + timedelta(minutes=40)) is None

    @pytest.mark.parametrize("label,expected", [
        ("33°C", (33, 33, "C")), ("26°C or below", (-math.inf, 26, "C")), ("60-61°F", (60, 61, "F")),
        ("36°C or higher", (36, math.inf, "C")),
    ])
    def test_bracket_range(self, label, expected):
        assert at.bracket_range(label) == expected

    def test_bracket_probabilities_sum_to_one_and_respect_observed(self):
        brackets = [at.bracket_range(x) for x in ("31°C or below", "32°C", "33°C", "34°C", "35°C or higher")]
        probs = [at.bracket_probability(b, 33.2, 0.8, "highest", 32.4, decimal_source=True) for b in brackets]
        assert abs(sum(probs) - 1) < 1e-6
        assert probs[0] == 0.0 and max(probs) == probs[2]  # 33°C paling mungkin, ≤31 mustahil

    def test_metar_rounding_slack(self):
        # max tercatat 23 (bacaan bulat) → bracket 23 masih sangat mungkin walau perkiraan 23.1
        p = at.bracket_probability(at.bracket_range("23°C"), 23.1, 0.6, "highest", 23, decimal_source=False)
        assert p > 0.5

    def test_vwap_walks_the_book(self):
        fill = vwap_for_usd([(0.50, 4), (0.52, 100)], 5.0)  # $2 di 50¢, sisa $3 di 52¢
        assert fill["worst"] == 0.52 and round(fill["price"], 4) == round(5 / (4 + 3 / 0.52), 4)
        assert vwap_for_usd([(0.5, 1)], 5.0) is None

    def test_taker_fee(self):
        assert at.taker_fee(0.5, 0.07) == pytest.approx(0.0175)


class TestRiskAndExecution:

    def test_execute_creates_paper_position_once(self, funded, sent):
        seed_market()
        order = at.execute(decision(), NOW)
        assert order is not None and order["strategy_version"] == "auto_btc_v1"
        assert Decimal(str(order["entry_price"])) == Decimal("0.567")  # VWAP 55¢ + fee 1.7¢
        assert at.execute(decision(), NOW) is None  # satu entri per market
        positions = get_open_positions()
        assert len(positions) == 1 and positions[0]["strategy_version"] == "auto_btc_v1"
        assert "🤖 AUTO BUY (paper) · BTC test" in sent[0] and "edge" in sent[0]

    def test_daily_cap_rejects_and_records(self, funded, sent, monkeypatch):
        monkeypatch.setattr(settings, "AUTOTRADE_MAX_DAILY_USD", Decimal("7"))
        seed_market("0xa")
        seed_market("0xb")
        assert at.execute(decision("btc|0xa", "0xa"), NOW) is not None
        assert at.execute(decision("btc|0xb", "0xb"), NOW) is None
        db = get_db_session()
        try:
            rejected = db.query(AutotradeDecision).filter_by(status="rejected").one()
            assert "batas harian" in rejected.reason
        finally:
            db.close()

    def test_daily_loss_stop(self, monkeypatch):
        monkeypatch.setattr(at, "today_summary", lambda now=None: {"day": "x", "spent": 0, "trades": 0,
                                                                    "realized_pnl": -25.0, "open_usd": 0})
        ok, reason = at.risk_check(5, NOW)
        assert not ok and "stop harian" in reason

    def test_open_exposure_cap(self, monkeypatch):
        monkeypatch.setattr(at, "today_summary", lambda now=None: {"day": "x", "spent": 0, "trades": 0,
                                                                    "realized_pnl": 0, "open_usd": 98.0})
        assert at.risk_check(5, NOW)[0] is False

    def test_insufficient_paper_balance_is_rejected_cleanly(self, sent):
        seed_market()
        assert at.execute(decision(size=30.0), NOW) is None  # saldo awal $20
        assert len(sent) == 1 and "Deposit" in sent[0]


class TestStrategies:

    def test_btc_tick_buys_side_with_edge(self, funded, sent, monkeypatch):
        hour = NOW.replace(minute=0, second=0)
        now = hour + timedelta(minutes=45)
        seed_market("0xbtc", at=now)
        monkeypatch.setattr(at, "_btc_market", lambda h, series="btc": {"condition_id": "0xbtc", "title": "Bitcoin Up or Down - test",
                                                          "up": "tu", "down": "td", "accepting": True, "slug": "s"})
        monkeypatch.setattr(at, "btc_model", lambda k, h, n, d=60: {"p_up": 0.80, "price": 1, "open": 1, "change_pct": 0.2,
                                                              "minutes_left": 15})
        monkeypatch.setattr(at, "_btc_klines", lambda symbol="BTCUSDT": [])
        books = {"tu": {"price": 0.62, "fee": 0.016, "spread": 0.01, "shares": 8},
                 "td": {"price": 0.39, "fee": 0.017, "spread": 0.01, "shares": 12}}
        monkeypatch.setattr(at, "_book_side", lambda token, usd: books[token])
        d = at.btc_tick(now)
        assert d["outcome"] == "UP" and round(d["edge"], 3) == round(0.80 - 0.636, 3)
        assert len(sent) == 1
        assert at.btc_tick(now) is None  # sudah diputuskan jam ini

    def test_btc_tick_outside_window_or_small_edge(self, monkeypatch):
        hour = NOW.replace(minute=0, second=0)
        assert at.btc_tick(hour + timedelta(minutes=10)) is None
        monkeypatch.setattr(at, "_btc_market", lambda h, series="btc": {"condition_id": "0xq", "title": "t", "up": "tu", "down": "td",
                                                          "accepting": True, "slug": "s"})
        monkeypatch.setattr(at, "_btc_klines", lambda symbol="BTCUSDT": [])
        monkeypatch.setattr(at, "btc_model", lambda k, h, n, d=60: {"p_up": 0.52, "price": 1, "open": 1, "change_pct": 0,
                                                              "minutes_left": 20})
        monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": 0.50, "fee": 0.0175, "spread": 0.01, "shares": 10})
        assert at.btc_tick(hour + timedelta(minutes=40)) is None

    def _underdog_setup(self, monkeypatch, cid="0xud"):
        hour = NOW.replace(minute=0, second=0)
        now = hour + timedelta(minutes=40)
        seed_market(cid, at=now)
        monkeypatch.setattr(at, "_btc_market", lambda h, series="btc": {"condition_id": cid, "title": "t", "up": "tu",
                                                                         "down": "td", "accepting": True, "slug": "s"})
        monkeypatch.setattr(at, "_btc_klines", lambda symbol="BTCUSDT": [])
        monkeypatch.setattr(at, "btc_model", lambda k, h, n, d=60: {"p_up": 0.35, "price": 1, "open": 1, "change_pct": -0.1,
                                                              "minutes_left": 20})
        books = {"tu": {"price": 0.22, "fee": 0.012, "spread": 0.01, "shares": 20},  # edge UP = 0.35 − 0.232
                 "td": {"price": 0.79, "fee": 0.012, "spread": 0.01, "shares": 6}}
        monkeypatch.setattr(at, "_book_side", lambda token, usd: books[token])
        return now

    def test_btc_tick_skips_cheap_underdog(self, funded, sent, monkeypatch):
        now = self._underdog_setup(monkeypatch)
        logged = []
        monkeypatch.setattr(at, "log_signal", lambda d, key, reason, now: logged.append(reason))
        assert at.btc_tick(now) is None and not sent
        assert logged == ["harga di bawah minimum (underdog)"]
        monkeypatch.setattr(at.settings, "AUTOTRADE_BTC_MIN_PRICE", 0.0)
        d = at.btc_tick(now)
        assert d is not None and d["outcome"] == "UP"

    def test_disabled_series_logs_shadow_signal_without_buying(self, funded, sent, monkeypatch):
        now = self._underdog_setup(monkeypatch, cid="0xsh")
        logged = []
        monkeypatch.setattr(at, "log_signal", lambda d, key, reason, now: logged.append(reason))
        assert at.btc_tick(now, shadow=True) is None and not sent
        assert logged == ["strategi nonaktif (shadow)"]

    def test_disabled_strategies_run_in_shadow(self, monkeypatch):
        calls = []
        monkeypatch.setattr(at, "is_enabled", lambda: True)
        monkeypatch.setattr(at, "enabled_strategies", lambda: ["btc"])
        monkeypatch.setattr(at, "btc_tick", lambda series, shadow=False: calls.append((series, shadow)))
        monkeypatch.setattr(at, "maker_place", lambda series: calls.append(("maker", series)))
        monkeypatch.setattr(at, "maker_manage", lambda: None)
        monkeypatch.setattr(at, "maybe_send_daily_report", lambda: None)
        at.run_autotrade_tick()
        assert calls == [("btc", False), ("btc15", True), ("btc5", True), ("eth", True), ("eth15", True)]

    def _event(self, favorite_first=True):
        markets = [
            {"market_id": "0x33", "bracket": "33°C", "price_yes": 0.55, "yes_token_id": "t33", "liquid": True,
             "polymarket_url": "u"},
            {"market_id": "0x32", "bracket": "32°C", "price_yes": 0.30, "yes_token_id": "t32", "liquid": True},
            {"market_id": "0x34", "bracket": "34°C", "price_yes": 0.10, "yes_token_id": "t34", "liquid": True},
        ]
        if not favorite_first:
            markets = [markets[1], markets[0], markets[2]]
        peak = (NOW + timedelta(hours=1)).astimezone(ZoneInfo("Asia/Tokyo"))
        return {"event_key": "Tokyo|highest|2026-09-30", "city": "Tokyo", "kind": "highest", "local_date": "2026-09-30",
                "peak_start": peak.isoformat(), "liquid": True, "markets": markets,
                "observation": {"station": "RJTT", "unit": "C", "value": 32.0,
                                "outlook": {"value": 33.1, "passed": False, "source": "Open-Meteo"}}}

    def test_weather_buys_agreeing_bracket_with_edge(self, monkeypatch):
        # perkiraan 33.1 ±1.2 (2 jam ke akhir puncak), terukur 32 → P(33°C) ≈ 35% > 25¢ + fee
        monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": 0.25, "fee": 0.0131, "spread": 0.02, "shares": 20})
        d = at.weather_decision(self._event(), NOW)
        assert d["market_id"] == "0x33" and d["side"] == "YES" and d["edge"] >= settings.AUTOTRADE_WEATHER_MIN_EDGE
        assert "sepakat dengan favorit pasar" in d["detail"] and "terukur 32°C" in d["detail"]

    def test_weather_skips_when_disagreeing_with_market(self, monkeypatch):
        monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": 0.25, "fee": 0.0131, "spread": 0.02, "shares": 20})
        assert at.weather_decision(self._event(favorite_first=False), NOW) is None
        monkeypatch.setattr(settings, "AUTOTRADE_WEATHER_REQUIRE_AGREEMENT", False)
        assert at.weather_decision(self._event(favorite_first=False), NOW)["market_id"] == "0x33"

    def test_weather_skips_expensive_or_no_estimate(self, monkeypatch):
        monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": 0.93, "fee": 0.004, "spread": 0.01, "shares": 5})
        assert at.weather_decision(self._event(), NOW) is None
        ev = self._event()
        ev["observation"]["outlook"] = None
        assert at.weather_decision(ev, NOW) is None


class TestControls:

    def test_enabled_flag_and_tick(self, monkeypatch):
        calls = []
        monkeypatch.setattr(at, "btc_tick", lambda now=None, series="btc", shadow=False:
                            calls.append(series + (" (shadow)" if shadow else "")))
        monkeypatch.setattr(at, "maker_place", lambda now=None, series="btc": calls.append(f"maker_{series}"))
        monkeypatch.setattr(at, "maker_manage", lambda now=None: calls.append("manage"))
        monkeypatch.setattr(at, "weather_tick", lambda now=None, phase="pre": calls.append(f"weather_{phase}"))
        monkeypatch.setattr(at, "maybe_send_daily_report", lambda now=None: False)
        monkeypatch.setattr(settings, "AUTOTRADE_ENABLED", False)
        at.run_autotrade_tick(include_weather=True)
        assert calls == []
        at.set_enabled(True)
        at.run_autotrade_tick(include_weather=True)
        # default: btc15 & maker_btc15 nonaktif → btc15 hanya mencatat sinyal (shadow)
        assert calls == ["btc", "maker_btc", "btc15 (shadow)", "btc5 (shadow)", "eth (shadow)", "eth15 (shadow)", "manage",
                         "weather_pre", "weather_post"]

    def test_telegram_start_stop_status(self):
        assert "🟢 Auto paper trader dijalankan" in handle_incoming_message("/startbot", sender_chat_id="1", allowed_chat_id="1")
        assert at.is_enabled() is True
        stop = handle_incoming_message("/stopbot", sender_chat_id="1", allowed_chat_id="1")
        assert "🔴" in stop and at.is_enabled() is False
        status = handle_incoming_message("/autostats 7", sender_chat_id="1", allowed_chat_id="1")
        assert "Hasil (7 hari)" in status and "btc:" in status and "weather:" in status
        assert "/startbot" in handle_incoming_message("/help", sender_chat_id="1", allowed_chat_id="1")

    def test_daily_report_once(self, sent, monkeypatch):
        monkeypatch.setattr(settings, "AUTOTRADE_REPORT_HOUR", 0)
        assert at.maybe_send_daily_report(NOW) is True
        assert at.maybe_send_daily_report(NOW) is False
        assert "Laporan harian auto trader" in sent[0]

    def test_api(self):
        from fastapi.testclient import TestClient
        from app.dashboard import app
        client = TestClient(app)
        assert client.post("/api/autotrade/start").json()["enabled"] is True
        data = client.get("/api/autotrade").json()
        assert data["enabled"] is True and "btc" in data["stats"] and data["limits"]["order_usd"] == 5.0
        assert client.post("/api/autotrade/stop").json()["enabled"] is False
        assert client.post("/api/autotrade/boom").status_code == 404
        assert 'id="autotradeContainer"' not in client.get("/").text and 'href="/autobot"' in client.get("/").text
        page = client.get("/autobot").text
        assert 'id="autotradeContainer"' in page and 'id="marketsContainer"' in page and "/api/autotrade/markets" in page


class TestBtc15AndMaker:

    def test_series_slugs_and_starts(self):
        t = datetime(2026, 9, 30, 2, 7, 30, tzinfo=timezone.utc)
        assert at.series_start("btc15", t) == datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)
        assert at.series_start("btc", t) == datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)
        assert at.series_start("btc15", t.replace(minute=52)) == datetime(2026, 9, 30, 2, 45, tzinfo=timezone.utc)
        assert at.series_slug("btc15", datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)) == "btc-updown-15m-1790733600"
        assert at.series_slug("btc", datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc)) == "bitcoin-up-or-down-september-29-2026-9pm-et"

    def test_btc15_uses_15_minute_horizon(self, funded, sent, monkeypatch):
        start = NOW.replace(minute=(NOW.minute // 15) * 15, second=0)
        now = start + timedelta(minutes=10)
        seed_market("0x15", at=now)
        seen = {}
        monkeypatch.setattr(at, "_btc_market", lambda s, series="btc": {"condition_id": "0x15", "title": "BTC 15m",
                                                                         "up": "tu", "down": "td", "accepting": True, "slug": "s"})
        monkeypatch.setattr(at, "_btc_klines", lambda symbol="BTCUSDT": [])
        monkeypatch.setattr(at, "btc_model", lambda k, s, n, d=60: seen.setdefault("d", d) and
                            {"p_up": 0.2, "price": 1, "open": 1, "change_pct": -0.1, "minutes_left": 5})
        monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": 0.70 if token == "tu" else 0.30,
                                                                   "fee": 0.015, "spread": 0.01, "shares": 10})
        d = at.btc_tick(now, series="btc15")
        assert seen["d"] == 15 and d["outcome"] == "DOWN" and d["strategy"] == "btc15"
        assert get_open_positions()[0]["strategy_version"] == "auto_btc15_v1"

    def _maker_setup(self, monkeypatch, p_up=0.70, book_ask=0.72):
        hour = NOW.replace(minute=0, second=0)
        now = hour + timedelta(minutes=20)
        seed_market("0xmk", at=now)
        state = {"books": {"tu": {"ask": book_ask, "bid": book_ask - 0.01}, "td": {"ask": 0.30, "bid": 0.29}},
                 "p_up": p_up}
        monkeypatch.setattr(at, "_btc_market", lambda s, series="btc": {"condition_id": "0xmk", "title": "BTC maker",
                                                                         "up": "tu", "down": "td", "accepting": True, "slug": "s"})
        monkeypatch.setattr(at, "_btc_klines", lambda symbol="BTCUSDT": [])
        monkeypatch.setattr(at, "btc_model", lambda k, s, n, d=60: {"p_up": state["p_up"], "price": 1, "open": 1,
                                                                    "change_pct": 0.1, "minutes_left": 40})
        monkeypatch.setattr("app.paper_trading.live_market_data.fetch_order_books",
                            lambda tokens: {t: state["books"][t] for t in tokens if t in state["books"]})
        return now, state

    def test_maker_places_below_fair_value_and_fills_on_trade_through(self, funded, sent, monkeypatch):
        now, state = self._maker_setup(monkeypatch)
        placed = at.maker_place(now, series="btc")
        assert placed["outcome"] == "UP" and placed["limit"] == 0.66  # floor(0.70 − 0.04)
        assert at.maker_place(now, series="btc") is None  # satu order per market
        assert at.maker_manage(now + timedelta(minutes=1)) == []  # ask 72¢ belum menembus
        state["books"]["tu"] = {"ask": 0.66, "bid": 0.65}
        assert at.maker_manage(now + timedelta(minutes=2)) == []  # ask sama dengan limit: antrian, belum terisi
        state["books"]["tu"] = {"ask": 0.65, "bid": 0.64}
        assert at.maker_manage(now + timedelta(minutes=3)) == []  # slippage 1¢: harus menembus > 1¢
        state["books"]["tu"] = {"ask": 0.64, "bid": 0.63}
        assert at.maker_manage(now + timedelta(minutes=4)) == ["maker|btc|0xmk:filled"]
        pos = get_open_positions()[0]
        assert pos["strategy_version"] == "auto_maker_btc_v1" and Decimal(str(pos["entry_price"])) == Decimal("0.66")
        assert "MAKER fill" in sent[-1]

    def test_maker_cancels_when_edge_disappears_and_expires(self, funded, monkeypatch):
        now, state = self._maker_setup(monkeypatch)
        at.maker_place(now, series="btc")
        state["p_up"] = 0.67  # edge 1¢ < setengah edge minimum
        assert at.maker_manage(now + timedelta(minutes=1)) == ["maker|btc|0xmk:cancelled"]
        assert at.reserved_usd() == 0

    def test_maker_expires_at_window_end(self, funded, monkeypatch):
        now, state = self._maker_setup(monkeypatch)
        at.maker_place(now, series="btc")
        assert at.reserved_usd() == 5.0
        assert at.maker_manage(now + timedelta(minutes=40)) == ["maker|btc|0xmk:expired"]


class TestWeatherPostPhase:

    def test_observation_window_spans_peak_and_after(self):
        from app.paper_trading import weather_peaks as wp
        w = wp.recommendation_window("Hong Kong", "highest", datetime(2026, 9, 30).date())
        post = wp.observation_window(w)
        assert post.start == w.peak_start and post.end == w.peak_end + timedelta(hours=settings.AUTOTRADE_WEATHER_POST_HOURS)
        assert post.contains(w.peak_end) and not w.contains(w.peak_end)

    def test_post_phase_uses_separate_strategy(self, monkeypatch):
        monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": 0.25, "fee": 0.0131, "spread": 0.02, "shares": 20})
        ev = TestStrategies()._event()
        d = at.weather_decision(ev, NOW, phase="post")
        assert d["strategy"] == "weather_post" and d["key"].startswith("weather_post|")


class TestNotifications:

    def test_rejection_notified_once_per_reason_per_day(self, sent):
        seed_market("0xr1")
        seed_market("0xr2")
        at.execute(decision("btc|0xr1", "0xr1", size=30.0), NOW)  # saldo paper $20 tidak cukup
        at.execute(decision("btc|0xr2", "0xr2", size=30.0), NOW)
        assert len(sent) == 1 and "AUTO TRADE DITOLAK" in sent[0] and "Deposit" in sent[0]

    def test_test_notification_reports_success_and_error(self, monkeypatch):
        monkeypatch.setattr(settings, "TELEGRAM_AUTOTRADE_CHAT_ID", "-100123")
        with patch("app.paper_trading.telegram.send_telegram_message", return_value={"success": True}) as fake:
            assert "✅ Pesan uji terkirim ke -100123" in handle_incoming_message("/tesnotif", sender_chat_id="1", allowed_chat_id="1")
        assert fake.call_args.kwargs["chat_id"] == "-100123"
        with patch("app.paper_trading.telegram.send_telegram_message", return_value={"success": False, "error": "chat not found"}):
            reply = handle_incoming_message("/tesnotif", sender_chat_id="1", allowed_chat_id="1")
        assert "❌ Gagal" in reply and "chat not found" in reply


def test_notification_has_no_polymarket_link_and_stats_have_total(funded, sent):
    seed_market()
    d = decision()
    d["url"] = "https://polymarket.com/event/x"
    at.execute(d, NOW)
    assert "polymarket.com" not in sent[0]
    status = handle_incoming_message("/autostats", sender_chat_id="1", allowed_chat_id="1")
    assert "*Total: 1 trade · belum ada yang selesai*" in status
    assert "/autostats" in handle_incoming_message("/help", sender_chat_id="1", allowed_chat_id="1")


class TestEditableConfig:

    def test_override_takes_effect_and_reset(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTOTRADE_MAX_DAILY_USD", Decimal("50"))
        assert at.cfg("MAX_DAILY_USD") == 50.0
        cfg = at.set_config({"MAX_DAILY_USD": 120, "STRATEGIES": ["weather", "btc15"], "WEATHER_REQUIRE_AGREEMENT": False})
        assert cfg["MAX_DAILY_USD"]["value"] == 120.0 and cfg["MAX_DAILY_USD"]["overridden"] is True
        assert at.enabled_strategies() == ["weather", "btc15"] and at.cfg("WEATHER_REQUIRE_AGREEMENT") is False
        monkeypatch.setattr(at, "today_summary", lambda now=None: {"day": "x", "spent": 100.0, "trades": 0,
                                                                    "realized_pnl": 0, "open_usd": 0})
        assert at.risk_check(5)[0] is True  # 105 ≤ 120 (default .env 50 akan menolak)
        at.set_config({"MAX_DAILY_USD": None})
        assert at.cfg("MAX_DAILY_USD") == 50.0
        at.set_config({"ORDER_USD": 9})
        assert at.reset_config()["ORDER_USD"]["overridden"] is False

    @pytest.mark.parametrize("updates,message", [
        ({"MAX_PRICE": 1.5}, "antara"), ({"ORDER_USD": "abc"}, "angka"), ({"STRATEGIES": "btc,moon"}, "tidak dikenal"),
        ({"BOGUS": 1}, "tidak dikenal"),
    ])
    def test_validation(self, updates, message):
        with pytest.raises(ValueError, match=message):
            at.set_config(updates)

    def test_config_api(self):
        from fastapi.testclient import TestClient
        from app.dashboard import app
        client = TestClient(app)
        data = client.put("/api/autotrade/config", json={"values": {"ORDER_USD": 7, "MAX_OPEN_USD": 200}}).json()
        assert data["limits"]["order_usd"] == 7.0 and data["limits"]["max_open_usd"] == 200.0
        assert data["config"]["ORDER_USD"]["overridden"] is True
        bad = client.put("/api/autotrade/config", json={"values": {"MAX_PRICE": 2}})
        assert bad.status_code == 400 and "antara" in bad.json()["detail"]
        assert client.delete("/api/autotrade/config").json()["config"]["ORDER_USD"]["overridden"] is False


def test_trade_history_shows_result_and_detail(funded, sent):
    from app.paper_trading.models import PaperPosition, PaperTrade, PaperTradeStatus, TradeSide
    seed_market("0xh1")
    seed_market("0xh2")
    d1 = decision(key="btc|0xh1", market_id="0xh1")
    d1["features"] = {"minute": 41.0, "change_pct": 0.12, "btc": 84000.0}
    at.execute(d1, NOW - timedelta(minutes=5))
    at.execute(decision(key="btc|0xh2", market_id="0xh2", prob=0.6, price=0.40, fee=0.017), NOW)
    db = get_db_session()
    pos = db.query(PaperPosition).filter_by(market_id="0xh1", strategy_version="auto_btc_v1").first()
    db.add(PaperTrade(account_id=pos.account_id, market_id="0xh1", side=TradeSide.YES, entry_price=Decimal("0.567"),
                      position_size=Decimal("5"), shares=pos.shares, exit_price=Decimal("1"), gross_pnl=Decimal("3.85"),
                      net_pnl=Decimal("3.85"), status=PaperTradeStatus.WON, closed_at=NOW, strategy_version="auto_btc_v1"))
    pos.shares = Decimal("0")  # posisi ditutup saat settle
    db.commit()
    db.close()
    rows = [r for r in at.trade_history(limit=50) if r["created_at"] >= NOW - timedelta(minutes=6)]
    won = next(r for r in at.trade_history(limit=50) if r["result"] == "MENANG" and r["pnl"] == 3.85)
    assert won["exit_price"] == 1.0 and won["detail"] == "detail" and won["outcome"] == "UP"
    assert sorted(r["result"] for r in rows) == ["MENANG", "TERBUKA"]
    text = handle_incoming_message("/autoriwayat 5 btc", sender_chat_id="1", allowed_chat_id="1")
    assert "Riwayat auto trade" in text and "✅ *MENANG* · PnL +3.85" in text and "⏳ *TERBUKA*" in text
    assert "/autoriwayat" in handle_incoming_message("/help", sender_chat_id="1", allowed_chat_id="1")


def test_history_detail_rebuilt_for_old_rows():
    assert "BTC 84,000.0 (+0.12% dari open)" in at._history_detail("btc", {"btc": 84000, "change_pct": 0.12,
                                                                           "minute": 41, "minutes_left": 19})
    assert at._history_detail("weather", {"estimate": 33.4, "kind": "highest", "unit": "C", "sigma": 0.6,
                                          "source": "HKO", "favorite": "33°C"}).startswith("Perkiraan max 33.4°C")


def test_hourly_report_only_to_group_and_once_per_hour(funded, monkeypatch):
    from app.paper_trading.models import PaperPosition, PaperTrade, PaperTradeStatus, TradeSide
    seed_market("0xhr")
    end = NOW.replace(minute=0, second=0)
    with patch("app.paper_trading.telegram.send_telegram_message", return_value={"success": True}):
        at.execute(decision(key="btc|0xhr", market_id="0xhr"), end - timedelta(minutes=30))
    db = get_db_session()
    pos = db.query(PaperPosition).filter_by(market_id="0xhr").first()
    db.add(PaperTrade(account_id=pos.account_id, market_id="0xhr", side=TradeSide.YES, entry_price=Decimal("0.567"),
                      position_size=Decimal("5"), shares=pos.shares, exit_price=Decimal("1"), gross_pnl=Decimal("3.8"),
                      net_pnl=Decimal("3.8"), status=PaperTradeStatus.WON, closed_at=end - timedelta(minutes=5),
                      strategy_version="auto_btc_v1"))
    db.commit()
    db.close()
    monkeypatch.setattr(settings, "TELEGRAM_AUTOTRADE_CHAT_ID", None)
    assert at.maybe_send_hourly_report(end + timedelta(minutes=1)) is False  # tanpa grup: tidak dikirim ke pribadi
    monkeypatch.setattr(settings, "TELEGRAM_AUTOTRADE_CHAT_ID", "-100")
    with patch("app.paper_trading.telegram.send_telegram_message", return_value={"success": True}) as send:
        assert at.maybe_send_hourly_report(end + timedelta(minutes=1)) is True
        assert at.maybe_send_hourly_report(end + timedelta(minutes=20)) is False  # sekali per jam
    text, kwargs = send.call_args[0][0], send.call_args[1]
    assert kwargs["chat_id"] == "-100"
    assert "⏱ Auto trade ·" in text and "Selesai 1 · WR 100% (1/1) · PnL +3.80" in text and "• btc: 1/1 menang" in text
    assert "Dibuka 1 trade ($5.00)" in text


def test_hourly_report_skipped_when_hour_is_empty(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_AUTOTRADE_CHAT_ID", "-100")
    later = NOW + timedelta(days=30)
    with patch("app.paper_trading.telegram.send_telegram_message") as send:
        assert at.maybe_send_hourly_report(later) is False
    assert not send.called


def test_slippage_added_to_taker_price_and_fee(monkeypatch):
    monkeypatch.setattr("app.paper_trading.live_market_data.fetch_order_books",
                        lambda tokens: {"tok": {"ask": 0.60, "bid": 0.59, "asks": [(0.60, 100.0)]}})
    monkeypatch.setattr("app.paper_trading.live_market_data.fetch_fee_rate", lambda token: 0.07)
    monkeypatch.setattr(settings, "AUTOTRADE_SLIPPAGE", 0.01)
    book = at._book_side("tok", 5.0)
    assert book["book_price"] == pytest.approx(0.60) and book["price"] == pytest.approx(0.61)
    assert book["slippage"] == pytest.approx(0.01) and book["fee"] == pytest.approx(0.07 * 0.61 * 0.39)
    monkeypatch.setattr(settings, "AUTOTRADE_SLIPPAGE", 0.0)
    assert at._book_side("tok", 5.0)["price"] == pytest.approx(0.60)


def test_btc5_series_slug_and_window():
    t = datetime(2026, 10, 3, 14, 37, 20, tzinfo=timezone.utc)
    start = at.series_start("btc5", t)
    assert start == datetime(2026, 10, 3, 14, 35, tzinfo=timezone.utc)
    assert at.series_slug("btc5", start) == f"btc-updown-5m-{int(start.timestamp())}"
    assert at.STRATEGY_VERSIONS["btc5"] == "auto_btc5_v1" and "maker_btc5" in at.STRATEGY_VERSIONS
    assert at._window(at.BTC_SERIES["btc5"]["window"]()) == (2.0, 4.0)


def test_stats_period_reset_keeps_old_data(funded, sent):
    from app.paper_trading.models import PaperPosition, PaperTrade, PaperTradeStatus, TradeSide
    seed_market("0xold")
    at.execute(decision(key="btc|0xold", market_id="0xold"), NOW - timedelta(hours=3))
    db = get_db_session()
    pos = db.query(PaperPosition).filter_by(market_id="0xold").first()
    db.add(PaperTrade(account_id=pos.account_id, market_id="0xold", side=TradeSide.YES, entry_price=Decimal("0.5"),
                      position_size=Decimal("5"), shares=pos.shares, exit_price=Decimal("0"), gross_pnl=Decimal("-5"),
                      net_pnl=Decimal("-5"), status=PaperTradeStatus.LOST, closed_at=NOW - timedelta(hours=2),
                      strategy_version="auto_btc_v1"))
    db.commit()
    db.close()
    try:
        at.set_stats_since(None)
        assert at.strategy_stats(now=NOW)["btc"]["settled"] >= 1
        assert "dimulai dari sekarang" in handle_incoming_message("/autostats reset", sender_chat_id="1", allowed_chat_id="1")
        assert at.stats_since() is not None
        fresh = at.strategy_stats()
        assert fresh["btc"]["settled"] == 0 and fresh["btc"]["trades"] == 0
        assert at.strategy_stats(all_time=True)["btc"]["settled"] >= 1  # data lama tetap ada
        text = handle_incoming_message("/autostats", sender_chat_id="1", allowed_chat_id="1")
        assert "*Hasil (sejak " in text
        assert "*Hasil (semua waktu)*" in handle_incoming_message("/autostats semua", sender_chat_id="1", allowed_chat_id="1")
        handle_incoming_message("/autostats sejak 2020-01-01", sender_chat_id="1", allowed_chat_id="1")
        assert at.stats_since().astimezone(ZoneInfo(settings.NOTIFY_TIMEZONE)).date().isoformat() == "2020-01-01"
        from fastapi.testclient import TestClient
        from app.dashboard import app
        data = TestClient(app).post("/api/autotrade/stats-since", json={"since": None}).json()
        assert data["stats_since"] is None and data["stats_label"] == "semua waktu"
        assert TestClient(app).post("/api/autotrade/stats-since", json={"since": "kemarin"}).status_code == 400
    finally:
        at.set_stats_since(None)


def test_btc5_uses_5_minute_horizon(funded, sent, monkeypatch):
    start = NOW.replace(minute=(NOW.minute // 5) * 5, second=0)
    now = start + timedelta(minutes=3)
    seed_market("0x5m", at=now)
    seen = {}
    monkeypatch.setattr(at, "_btc_market", lambda s, series="btc": seen.setdefault("series", series) and
                        {"condition_id": "0x5m", "title": "BTC 5m", "up": "tu", "down": "td", "accepting": True, "slug": "s"})
    monkeypatch.setattr(at, "_btc_klines", lambda symbol="BTCUSDT": [])
    monkeypatch.setattr(at, "btc_model", lambda k, s, n, d=60: seen.setdefault("d", d) and
                        {"p_up": 0.85, "price": 1, "open": 1, "change_pct": 0.1, "minutes_left": 2})
    monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": 0.70 if token == "tu" else 0.31,
                                                               "fee": 0.015, "spread": 0.01, "shares": 10})
    d = at.btc_tick(now, series="btc5")
    assert seen == {"series": "btc5", "d": 5} and d["outcome"] == "UP" and d["strategy"] == "btc5"
    assert get_open_positions()[0]["strategy_version"] == "auto_btc5_v1"


def test_eth_series_slugs_klines_and_trade(funded, sent, monkeypatch):
    start = datetime(2026, 10, 4, 10, 30, tzinfo=timezone.utc)
    assert at.series_slug("eth15", start) == f"eth-updown-15m-{int(start.timestamp())}"
    assert at.series_slug("eth", datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)) == "ethereum-up-or-down-october-4-2026-6am-et"
    assert at.series_slug("btc", datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)) == "bitcoin-up-or-down-october-4-2026-6am-et"
    assert at.STRATEGY_VERSIONS["eth15"] == "auto_eth15_v1" and "maker_eth" in at.STRATEGY_VERSIONS
    seen = {}
    now = NOW.replace(minute=(NOW.minute // 15) * 15, second=0) + timedelta(minutes=10)
    seed_market("0xeth", at=now)
    monkeypatch.setattr(at, "_btc_market", lambda s, series="btc": {"condition_id": "0xeth", "title": "ETH 15m",
                                                                     "up": "tu", "down": "td", "accepting": True, "slug": "s"})
    monkeypatch.setattr(at, "_btc_klines", lambda symbol="BTCUSDT": seen.setdefault("symbol", symbol) and [])
    monkeypatch.setattr(at, "btc_model", lambda k, s, n, d=60: {"p_up": 0.85, "price": 2400.5, "open": 2390.0,
                                                                "change_pct": 0.44, "minutes_left": 5})
    monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": 0.70 if token == "tu" else 0.31,
                                                               "fee": 0.015, "spread": 0.01, "shares": 10})
    d = at.btc_tick(now, series="eth15")
    assert seen["symbol"] == "ETHUSDT" and d["strategy"] == "eth15" and d["detail"].startswith("ETH 2,400.5")
    assert get_open_positions()[0]["strategy_version"] == "auto_eth15_v1"
    row = next(r for r in at.trade_history(limit=50) if r["strategy"] == "eth15")
    assert row["detail"].startswith("ETH 2,400.5")


def test_crypto_markets_overview(monkeypatch):
    now = datetime(2026, 10, 4, 10, 40, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(at, "enabled_strategies", lambda: ["btc", "eth15", "maker_eth15"])
    monkeypatch.setattr(at, "_btc_market", lambda start, series="btc": None if series == "btc5" else
                        {"condition_id": f"0x{series}", "title": f"T {series}", "up": f"u{series}", "down": f"d{series}",
                         "accepting": True, "slug": f"s-{series}"})
    monkeypatch.setattr(at, "_btc_klines", lambda symbol="BTCUSDT": [])
    monkeypatch.setattr(at, "btc_model", lambda k, s, n, d=60: {"p_up": 0.7, "price": 100.0, "open": 99.0,
                                                                "change_pct": 1.0, "minutes_left": 3.0})
    monkeypatch.setattr("app.paper_trading.live_market_data.fetch_order_books",
                        lambda tokens: {t: {"ask": 0.60 if t.startswith("u") else 0.42, "bid": 0.58} for t in tokens})
    monkeypatch.setattr("app.paper_trading.live_market_data.fetch_fee_rate", lambda token: 0.07)
    monkeypatch.setattr(settings, "AUTOTRADE_SLIPPAGE", 0.01)
    rows = {r["series"]: r for r in at.crypto_markets_overview(now)}
    assert set(rows) == {"btc", "btc15", "btc5", "eth", "eth15"}
    assert rows["btc5"]["error"] == "market belum tersedia"
    eth15 = rows["eth15"]
    assert eth15["asset"] == "eth" and eth15["label"] == "ETH 15 menit" and eth15["taker_on"] and eth15["maker_on"]
    assert eth15["minute"] == 10.5 and eth15["in_window"] is True  # jendela 15 menit = menit 7–14
    up = eth15["sides"]["UP"]
    assert up["cost"] == pytest.approx(0.61 + 0.07 * 0.61 * 0.39) and up["edge"] == pytest.approx(0.7 - up["cost"])
    assert rows["btc"]["in_window"] is True and rows["eth"]["taker_on"] is False
    assert up["skip"] is None  # 61¢ + fee, edge ≈ 8¢, dalam jendela
    assert eth15["sides"]["DOWN"]["skip"] == "edge < 5¢"


def test_pnl_calendar_days_months_and_trades(funded, sent):
    from zoneinfo import ZoneInfo as _Z
    from app.paper_trading.models import PaperPosition, PaperTrade, PaperTradeStatus, TradeSide
    wib = _Z("Asia/Jakarta")
    seed_market("0xcal1")
    seed_market("0xcal2")
    d1 = decision(key="btc|0xcal1", market_id="0xcal1")
    d1["features"] = {"btc": 84000.0, "change_pct": 0.1, "minute": 40, "minutes_left": 20}
    at.execute(d1, NOW)
    d2 = decision(key="eth15|0xcal2", market_id="0xcal2")
    d2["strategy"] = "eth15"
    at.execute(d2, NOW)
    db = get_db_session()
    acc = db.query(PaperPosition).filter_by(market_id="0xcal1").first().account_id
    closes = [("0xcal1", "auto_btc_v1", 3.5, datetime(2031, 3, 4, 23, 30, tzinfo=wib)),   # 4 Mar WIB (16:30 UTC)
              ("0xcal2", "auto_eth15_v1", -2.0, datetime(2031, 3, 5, 1, 0, tzinfo=wib)),   # 5 Mar WIB (4 Mar UTC!)
              ("0xcal1", "auto_btc_v1", 1.0, datetime(2031, 4, 2, 9, 0, tzinfo=wib))]
    for mid, ver, pnl, at_ in closes:
        db.add(PaperTrade(account_id=acc, market_id=mid, side=TradeSide.YES, entry_price=Decimal("0.5"),
                          position_size=Decimal("2"), shares=Decimal("4"), exit_price=Decimal("1" if pnl > 0 else "0"),
                          gross_pnl=Decimal(str(pnl)), net_pnl=Decimal(str(pnl)),
                          status=PaperTradeStatus.WON if pnl > 0 else PaperTradeStatus.LOST,
                          closed_at=at_.astimezone(timezone.utc), strategy_version=ver, market_name=f"M {mid}"))
    db.commit()
    db.close()

    cal = at.pnl_calendar("2031-03")
    assert cal["days"]["2031-03-04"]["pnl"] == 3.5 and cal["days"]["2031-03-05"]["pnl"] == -2.0  # tanggal WIB
    assert cal["totals"]["pnl"] == 1.5 and cal["totals"]["green"] == 1 and cal["totals"]["red"] == 1
    assert list(at.pnl_calendar("2031-03", strategy="eth_all")["days"]) == ["2031-03-05"]
    year = at.pnl_calendar_year(2031)
    assert year["months"]["2031-03"]["trades"] == 2 and year["months"]["2031-04"]["pnl"] == 1.0
    assert year["totals"]["pnl"] == 2.5

    trades = at.closed_trades(day="2031-03-04")
    assert len(trades) == 1 and trades[0]["strategy"] == "btc" and trades[0]["result"] == "MENANG"
    assert trades[0]["pnl"] == 3.5 and trades[0]["detail"] == "detail" and trades[0]["prob"] == 0.7
    assert [t["strategy"] for t in at.closed_trades(month="2031-03")] == ["eth15", "btc"]  # terbaru dulu

    from fastapi.testclient import TestClient
    from app.dashboard import app
    client = TestClient(app)
    assert client.get("/api/autotrade/calendar", params={"month": "2031-03"}).json()["totals"]["trades"] == 2
    assert client.get("/api/autotrade/calendar", params={"year": 2031, "strategy": "btc_all"}).json()["totals"]["pnl"] == 4.5
    assert client.get("/api/autotrade/calendar", params={"month": "2031-13"}).status_code == 400
    assert client.get("/api/autotrade/calendar", params={"month": "2031-03", "strategy": "moon"}).status_code == 400
    assert len(client.get("/api/autotrade/calendar/trades", params={"month": "2031-04"}).json()["trades"]) == 1
    assert client.get("/api/autotrade/calendar/trades").status_code == 400
    assert 'id="calGrid"' in client.get("/autobot").text
