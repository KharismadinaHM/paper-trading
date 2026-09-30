"""
Test alert lonjakan suhu Hong Kong (HKO real-time): parsing data, deteksi lonjakan & derajat baru,
throttle, perkiraan max, dan command /hk.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.core.database import get_db_session
from app.paper_trading import hko_alerts as hk
from app.paper_trading.models import StationAlert, StationReading
from app.paper_trading.telegram_bot import handle_incoming_message

HKT = ZoneInfo("Asia/Hong_Kong")


def hkt(hour, minute=0):
    return datetime(2026, 9, 29, hour, minute, tzinfo=HKT)


MARKET = [{"bracket": "33°C", "yes_token_id": "t33", "price_yes": 0.45, "ask": 0.47, "bid": 0.44},
          {"bracket": "32°C", "yes_token_id": "t32", "price_yes": 0.35, "ask": 0.36, "bid": 0.34},
          {"bracket": "34°C or higher", "yes_token_id": "t34", "price_yes": 0.1, "ask": 0.12, "bid": 0.09}]


@pytest.fixture
def env(monkeypatch):
    """Bacaan HKO disuplai dari daftar; prakiraan & market dipalsukan; Telegram direkam."""
    state = {"reading": None, "sent": []}
    monkeypatch.setattr(hk, "fetch_hko_reading", lambda: state["reading"])
    flat = [(hkt(h).astimezone(timezone.utc), 31.0) for h in range(24)]  # prakiraan datar 31°C
    monkeypatch.setattr("app.paper_trading.weather_outlook.fetch_hourly_forecast",
                        lambda lat, lon: state.get("forecast", flat))
    monkeypatch.setattr(hk, "_today_market", lambda now, kind="highest": [dict(m) for m in MARKET] if kind == "highest" else [])
    monkeypatch.setattr(hk, "hko_official_forecast", lambda: state.get("official"))
    monkeypatch.setattr(settings, "HKO_ALERTS", True)

    def send(text, **kw):
        state["sent"].append(text)
        return {"success": True}

    patcher = patch("app.paper_trading.telegram.send_telegram_message", side_effect=send)
    patcher.start()
    yield state
    patcher.stop()


def step(env, at, temp, max_=None, min_=28.0):
    env["reading"] = {"observed_at": at, "temp": temp, "max": max_ if max_ is not None else temp, "min": min_}
    return hk.check_hko_alerts(now=at.astimezone(timezone.utc) + timedelta(minutes=2))


def test_parse_hko_csv():
    temp_csv = "Date time,Automatic Weather Station,Air Temperature(degree Celsius)\n202609291140,HK Observatory,31.7\n"
    maxmin_csv = ("Date time,Automatic Weather Station,Max,Min\n202609291140,Chek Lap Kok,33,26\n"
                  "202609291140,HK Observatory,31.9,28.7\n")
    with patch("app.paper_trading.live_market_data._http",
               side_effect=lambda url, **kw: temp_csv if "1min" in url else maxmin_csv):
        r = hk.fetch_hko_reading()
    assert r == {"observed_at": hkt(11, 40), "temp": 31.7, "max": 31.9, "min": 28.7}


def test_spike_alert_with_estimate_and_market(env):
    assert step(env, hkt(11, 0), 31.0) is None
    assert step(env, hkt(11, 10), 31.3) is None
    text = step(env, hkt(11, 20), 32.0)  # +1.0 dalam 30 menit
    assert text is not None and len(env["sent"]) == 1
    assert "🚨 #HongKong suhu melonjak +1.0°C dalam 30 menit" in text
    assert "Sekarang 32.0°C (HKO 11:20 HKT / 10:20 WIB)" in text
    assert "Proyeksi tren (+" in text and "Perkiraan max hari ini ±" in text
    assert "Market: " in text and "33°C ask 47¢" in text


def test_spike_cooldown(env, monkeypatch):
    monkeypatch.setattr(settings, "HKO_NEAR_DEGREE_FRACTION", 0.99)  # uji lonjakan saja
    for minute, temp in ((0, 30.0), (10, 30.5), (20, 31.0)):
        step(env, hkt(10, minute), temp)
    assert len(env["sent"]) == 1
    step(env, hkt(10, 30), 31.9)  # masih dalam jeda 30 menit
    assert len(env["sent"]) == 1
    step(env, hkt(11, 0), 32.9)
    assert len(env["sent"]) == 2


def test_new_whole_degree_alert_once(env, monkeypatch):
    monkeypatch.setattr(settings, "HKO_ALERT_SPIKE_DEGREES", 5.0)  # hanya uji derajat baru
    monkeypatch.setattr(settings, "HKO_NEAR_DEGREE_FRACTION", 0.99)
    step(env, hkt(13, 0), 32.8)
    text = step(env, hkt(13, 10), 33.0)
    assert "🔺 #HongKong max hari ini menembus 33°C (32.8 → 33.0°C)" in text
    # 13:10 HKT: belum boleh dianggap final (HKO_FINAL_HOUR) dan tren belum cukup → tidak menebak bracket
    assert "belum bisa dihitung" in text and "👉" not in text
    step(env, hkt(13, 20), 32.9, max_=33.0)
    step(env, hkt(13, 30), 33.2)
    assert len(env["sent"]) == 1  # 33°C sudah dialertkan hari ini


def test_no_alert_outside_hours_or_when_disabled(env, monkeypatch):
    step(env, hkt(22, 0), 28.0)
    assert step(env, hkt(22, 10), 30.0) is None
    monkeypatch.setattr(settings, "HKO_ALERTS", False)
    assert step(env, hkt(12, 0), 35.0) is None
    assert env["sent"] == []


def test_stale_reading_does_not_alert(env):
    step(env, hkt(11, 0), 31.0)
    env["reading"] = {"observed_at": hkt(11, 10), "temp": 32.5, "max": 32.5, "min": 28.0}
    assert hk.check_hko_alerts(now=hkt(11, 45).astimezone(timezone.utc)) is None


def test_readings_and_alerts_are_stored(env):
    step(env, hkt(11, 0), 31.0)
    step(env, hkt(11, 20), 32.0)
    db = get_db_session()
    try:
        assert db.query(StationReading).count() == 2
        # 31.0 → 32.0 = lonjakan +1.0 sekaligus menembus 32°C
        assert sorted(a.kind for a in db.query(StationAlert)) == ["degree", "spike"]
    finally:
        db.close()


@pytest.mark.parametrize("value,bracket", [(32.8, "32°C"), (33.0, "33°C"), (34.6, "34°C or higher"), (30.1, None)])
def test_bracket_for_hko_decimal(value, bracket):
    assert hk.bracket_for(value, [m["bracket"] for m in MARKET]) == bracket


def test_hk_command(env):
    now = datetime.now(HKT).replace(second=0, microsecond=0)
    env["reading"] = {"observed_at": now, "temp": 31.4, "max": 31.6, "min": 28.1}
    reply = handle_incoming_message("/hk", sender_chat_id="1", allowed_chat_id="1")
    assert "Hong Kong · HKO real-time" in reply and "Sekarang 31.4°C" in reply and "max hari ini 31.6°C" in reply
    assert "/hk" in handle_incoming_message("/help", sender_chat_id="1", allowed_chat_id="1")


def test_without_forecast_or_trend_estimate_is_unknown(env):
    env["forecast"] = []
    env["reading"] = {"observed_at": hkt(11, 50), "temp": 31.6, "max": 31.8, "min": 28.7}
    status = hk.hko_status(now=hkt(11, 52).astimezone(timezone.utc))
    assert status["estimate"] is None and status["peak_passed"] is False
    text = hk.format_hko_message(status, [])
    assert "belum bisa dihitung" in text and "Puncak kemungkinan sudah lewat" not in text


def test_official_very_hot_forecast_drives_estimate(env):
    env["official"] = {"text": "Mainly fine. Very hot in the afternoon.", "period": "this afternoon and tonight",
                       "max_hint": 33.0, "very_hot_warning": True}
    env["reading"] = {"observed_at": hkt(11, 50), "temp": 31.6, "max": 31.8, "min": 28.7}
    status = hk.hko_status(now=hkt(11, 52).astimezone(timezone.utc))
    assert status["estimate"] == 33.0
    text = hk.format_hko_message(status, [])
    assert "Prakiraan resmi HKO: \"Mainly fine. Very hot in the afternoon.\" (≈33°C)" in text
    assert "Very Hot Weather Warning sedang berlaku" in text and "(bracket ≈ 33°C)" in text


@pytest.mark.parametrize("period,desc,hint", [
    ("Weather forecast for this afternoon and tonight", "Mainly fine. Very hot in the afternoon.", 33.0),
    ("Weather forecast for today", "Sunny. Maximum temperature around 35 degrees.", 35.0),
    ("Weather forecast for tonight and tomorrow", "Very hot tomorrow.", None),
])
def test_official_forecast_parsing(period, desc, hint):
    import json
    from app.paper_trading import live_market_data as live
    live.clear_cache()
    flw = json.dumps({"forecastPeriod": period, "forecastDesc": desc})
    with patch.object(live, "_http", side_effect=lambda url, **kw: flw if "flw" in url else '{"WHOT": {}}'):
        result = hk.hko_official_forecast()
    live.clear_cache()
    assert result["max_hint"] == hint and result["very_hot_warning"] is True



class TestLateRiseCase:
    """Kasus 30 Sep: 33°C 98.5¢ pukul 14:40, HKO naik ke 34.2°C sekitar 15:50."""

    MARKET = [{"bracket": "33°C", "yes_token_id": "t33", "price_yes": 0.9, "ask": 0.91, "bid": 0.9},
              {"bracket": "34°C", "yes_token_id": "t34", "price_yes": 0.15, "ask": 0.16, "bid": 0.14},
              {"bracket": "35°C or higher", "yes_token_id": "t35", "price_yes": 0.01, "ask": 0.02, "bid": 0.01}]

    def test_near_next_degree_alert_before_final_hour(self, env, monkeypatch):
        monkeypatch.setattr(hk, "_today_market", lambda now, kind="highest": [dict(m) for m in self.MARKET] if kind == "highest" else [])
        monkeypatch.setattr(settings, "HKO_ALERT_SPIKE_DEGREES", 5.0)
        step(env, hkt(15, 0), 33.5)
        text = step(env, hkt(15, 10), 33.8)
        assert "⚠️ #HongKong max 33.8°C — tinggal 0.2°C ke 34°C (bracket 34°C ask 16¢)" in text
        assert "Belum final sebelum 17:00 HKT" in text
        step(env, hkt(15, 20), 33.9)
        assert len(env["sent"]) == 1  # sekali per derajat per hari

    def test_not_final_before_17_even_after_peak(self, env):
        step(env, hkt(15, 0), 33.4)
        status = hk.hko_status(now=hkt(15, 32).astimezone(timezone.utc))
        env["reading"] = {"observed_at": hkt(15, 30), "temp": 33.3, "max": 33.4, "min": 28.0}
        status = hk.hko_status(now=hkt(15, 32).astimezone(timezone.utc))
        assert status["final_ok"] is False and status["peak_passed"] is False
        assert "Puncak kemungkinan sudah lewat" not in hk.format_hko_message(status, [])
        assert hk.can_be_final(hkt(17, 5).astimezone(timezone.utc), 33.3, 33.4) is True
        assert hk.can_be_final(hkt(15, 30).astimezone(timezone.utc), 32.3, 33.4) is True  # sudah turun ≥1°C

    def test_position_risk_alert(self, env, monkeypatch):
        monkeypatch.setattr(hk, "_today_market", lambda now, kind="highest": [dict(m) for m in self.MARKET] if kind == "highest" else [])
        monkeypatch.setattr(settings, "HKO_ALERT_SPIKE_DEGREES", 5.0)
        monkeypatch.setattr(settings, "HKO_NEAR_DEGREE_FRACTION", 0.7)
        monkeypatch.setattr(hk, "held_hk_positions", lambda now: [{"bracket": "33°C", "shares": 12.0, "source": "wallet"}])
        step(env, hkt(15, 0), 33.5)
        text = step(env, hkt(15, 10), 33.8)
        assert "🛑 Posisi wallet Anda: 33°C YES (12.0 shares) berisiko — max 33.8°C, tinggal 0.2°C ke 34°C" in text
        assert "33°C ask 91¢" in text

    def test_held_positions_from_wallet_and_paper(self, monkeypatch):
        from datetime import date
        today = datetime.now(HKT).date()
        title = f"Will the highest temperature in Hong Kong be 33°C on {today:%B} {today.day}?"
        monkeypatch.setattr(settings, "POLYMARKET_WALLET_ADDRESS", "0x" + "e" * 40)
        monkeypatch.setattr("app.paper_trading.wallets._get", lambda path, **kw: [
            {"title": title, "outcome": "Yes", "size": 12},
            {"title": title.replace("33°C", "34°C"), "outcome": "No", "size": 3},
            {"title": "Will the highest temperature in Tokyo be 25°C?", "outcome": "Yes", "size": 1}])
        monkeypatch.setattr("app.paper_service.get_open_positions", lambda: [
            {"market_name": title.replace("33°C", "34°C"), "side": "YES", "shares": 5}])
        held = hk.held_hk_positions(datetime.now(timezone.utc))
        assert {(h["bracket"], h["source"]) for h in held} == {("33°C", "wallet"), ("34°C", "paper")}


def test_history_command_and_csv(env):
    for minute, temp in ((0, 31.0), (10, 31.4), (20, 32.1)):
        step(env, hkt(9, minute), temp)
    text = hk.format_history(datetime(2026, 9, 29).date())
    assert "Riwayat HKO" in text and "`09:10  31.4  31.4  +0.4`" in text and "Max hari ini 32.1°C" in text
    reply = handle_incoming_message("/hk riwayat 2026-09-29", sender_chat_id="1", allowed_chat_id="1")
    assert "Riwayat HKO* 29 Sep 2026" in reply
    assert "Format tanggal" in handle_incoming_message("/hk riwayat kemarin", sender_chat_id="1", allowed_chat_id="1")
    csv_text = hk.readings_csv(datetime(2026, 9, 29).date())
    assert csv_text.splitlines()[0].startswith("observed_at_hkt,temp_c") and len(csv_text.strip().splitlines()) == 4
    from fastapi.testclient import TestClient
    from app.dashboard import app
    assert TestClient(app).get("/api/hk/readings.csv?date=2026-09-29").text == csv_text
    assert TestClient(app).get("/api/hk/readings.csv?date=bad").status_code == 400


class TestMinPrediction:

    MIN_MARKET = [{"bracket": "27°C", "yes_token_id": "l27", "price_yes": 0.6, "ask": 0.62, "bid": 0.6},
                  {"bracket": "28°C", "yes_token_id": "l28", "price_yes": 0.3, "ask": 0.31, "bid": 0.3}]

    def test_min_section_with_forecast_dropping_tonight(self, env, monkeypatch):
        # prakiraan: malam ini lebih dingin (26.5 pukul 23:00) → min belum final
        env["forecast"] = [(hkt(h).astimezone(timezone.utc), 31.0 if h < 18 else 26.5) for h in range(24)]
        monkeypatch.setattr(hk, "_today_market", lambda now, kind="highest":
                            [dict(m) for m in (MARKET if kind == "highest" else self.MIN_MARKET)])
        env["reading"] = {"observed_at": hkt(12, 0), "temp": 31.0, "max": 31.2, "min": 28.3}
        status = hk.hko_status(now=hkt(12, 2).astimezone(timezone.utc))
        assert status["min"] == 28.3 and status["min_estimate"] < 28.3
        text = hk.format_hko_message(status, [])
        assert "❄️ Min hari ini tercatat 28.3°C" in text and "Perkiraan min hari ini ±" in text
        assert "bisa turun sampai tengah malam" in text and "Market min: " in text

    def test_min_kept_when_rest_of_day_warmer(self, env):
        env["reading"] = {"observed_at": hkt(12, 0), "temp": 31.0, "max": 31.2, "min": 28.3}
        status = hk.hko_status(now=hkt(12, 2).astimezone(timezone.utc))  # prakiraan datar 31°C
        assert status["min_estimate"] == 28.3
        assert "min kemungkinan tetap 28.3°C" in hk.format_hko_message(status, [])

    def test_official_min_hint_parsed(self):
        import json
        from app.paper_trading import live_market_data as live
        live.clear_cache()
        flw = json.dumps({"forecastPeriod": "Weather forecast for tonight and tomorrow",
                          "forecastDesc": "Fine. Minimum temperature around 26 degrees."})
        with patch.object(live, "_http", side_effect=lambda url, **kw: flw if "flw" in url else "{}"):
            result = hk.hko_official_forecast()
        live.clear_cache()
        assert result["min_hint"] == 26.0 and result["max_hint"] is None


def test_tomorrow_forecast_numbers_are_ignored():
    import json
    from app.paper_trading import live_market_data as live
    live.clear_cache()
    flw = json.dumps({"forecastPeriod": "Weather forecast for this afternoon and tonight",
                      "forecastDesc": "Mainly fine. The minimum temperature will be about 29 degrees tomorrow. "
                                      "The maximum temperature will be around 35 degrees tomorrow."})
    with patch.object(live, "_http", side_effect=lambda url, **kw: flw if "flw" in url else "{}"):
        result = hk.hko_official_forecast()
    live.clear_cache()
    assert result["min_hint"] is None and result["max_hint"] is None
