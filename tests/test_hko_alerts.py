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
    monkeypatch.setattr(hk, "_today_market", lambda now: [dict(m) for m in MARKET])
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


def test_spike_cooldown(env):
    for minute, temp in ((0, 30.0), (10, 30.5), (20, 31.0)):
        step(env, hkt(10, minute), temp)
    assert len(env["sent"]) == 1
    step(env, hkt(10, 30), 31.9)  # masih dalam jeda 30 menit
    assert len(env["sent"]) == 1
    step(env, hkt(11, 0), 32.9)
    assert len(env["sent"]) == 2


def test_new_whole_degree_alert_once(env, monkeypatch):
    monkeypatch.setattr(settings, "HKO_ALERT_SPIKE_DEGREES", 5.0)  # hanya uji derajat baru
    step(env, hkt(13, 0), 32.8)
    text = step(env, hkt(13, 10), 33.0)
    assert "🔺 #HongKong max hari ini menembus 33°C (32.8 → 33.0°C)" in text
    assert "(bracket ≈ 33°C)" in text and "👉 33°C" in text
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
