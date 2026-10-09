"""Test prakiraan HK (cuaca terkini, per jam, 9 hari), model besok, AI per jam & besok, riwayat trade HK."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.core.database import get_db_session
from app.paper_trading import hk_ai, hk_bot, hk_forecast
from app.paper_trading.models import HkForecastView

HKT = ZoneInfo("Asia/Hong_Kong")
NOW = datetime(2026, 10, 9, 14, 0, tzinfo=HKT).astimezone(timezone.utc)

RHRREAD = {"updateTime": "2026-10-09T14:02:00+08:00", "icon": [51],
           "temperature": {"data": [{"place": "Hong Kong Observatory", "value": 30}]},
           "humidity": {"data": [{"place": "Hong Kong Observatory", "value": 66}]},
           "rainfall": {"data": [{"place": "Central", "max": 0}, {"place": "Sha Tin", "max": 2}]},
           "uvindex": {"data": [{"value": 7, "desc": "high"}]}, "warningMessage": ["Very Hot Weather Warning is in force."]}
FND = {"generalSituation": "A ridge of high pressure...", "updateTime": "2026-10-09T11:30:00+08:00",
       "weatherForecast": [{"forecastDate": "20261010", "week": "Saturday", "forecastWeather": "Sunny periods.",
                            "forecastMaxtemp": {"value": 31}, "forecastMintemp": {"value": 26}, "ForecastIcon": 51, "PSR": "Low"},
                           {"forecastDate": "20261011", "week": "Sunday", "forecastWeather": "Showers.",
                            "forecastMaxtemp": {"value": 29}, "forecastMintemp": {"value": 25}, "ForecastIcon": 62, "PSR": "High"}]}


def open_meteo():
    start = datetime(2026, 10, 9, 0, tzinfo=timezone.utc)  # 08:00 HKT
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(72)]
    temps = [27 + 3 * ((i + 8) % 24 in range(11, 17)) for i in range(72)]  # 30°C jam 11–16 HKT
    return {"hourly": {"time": times, "temperature_2m": temps, "weather_code": [2] * 72,
                       "precipitation_probability": [20] * 72, "relative_humidity_2m": [70] * 72,
                       "wind_speed_10m": [12.0] * 72, "cloud_cover": [40] * 72}}


@pytest.fixture
def http(monkeypatch):
    def fake(url, data=None, timeout=10):
        if "rhrread" in url:
            return json.dumps(RHRREAD)
        if "dataType=fnd" in url:
            return json.dumps(FND)
        if "open-meteo" in url:
            return json.dumps(open_meteo())
        raise AssertionError(url)
    monkeypatch.setattr("app.paper_trading.live_market_data._http", fake)


def test_current_weather_nine_day_and_hourly(http):
    now = hk_forecast.current_weather()
    assert now["text"] == "Cerah berawan" and now["humidity"] == 66 and now["rain_max_mm"] == 2 and now["uv"] == 7
    assert now["warnings"] == ["Very Hot Weather Warning is in force."]
    days = hk_forecast.nine_day()["days"]
    assert days[0] == {**days[0], "date": "2026-10-10", "max": 31, "min": 26, "text": "Cerah berawan", "psr": "Low"}
    hours = hk_forecast.hourly_outlook(NOW, hours=24)
    assert hours[0]["hour"] == "15:00" and hours[0]["temp"] == 30 and hours[0]["text"] == "Berawan sebagian"
    assert hours[5]["icon"] == "⛅" and len(hours) == 24 and hours[-1]["date"] == "2026-10-10"


def test_tomorrow_model_blends_hourly_and_hko(http, monkeypatch):
    monkeypatch.setattr("app.paper_trading.hko_alerts._today_market", lambda now, kind="highest", day=None: [])
    t = hk_bot.analyze_tomorrow(NOW)
    assert t["day"] == "2026-10-10" and t["official"]["max"] == 31
    assert t["max"]["projected"] == 30 and t["max"]["mu"] == pytest.approx(30.5)   # (30 + 31) / 2
    assert t["min"]["projected"] == 27 and t["min"]["mu"] == pytest.approx(26.5)
    assert t["max"]["observed"] is None and t["max"]["sigma"] >= hk_bot.SIGMA_TOMORROW_FLOOR
    assert sum(b["model"] for b in t["max"]["brackets"]) == pytest.approx(1, abs=1e-3)
    assert t["max"]["brackets"][0]["bracket"].endswith("or below") and t["max"]["sigma"] == hk_bot.SIGMA_TOMORROW_FLOOR


def test_ai_view_parses_tomorrow_and_hourly_and_records(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "k")
    ctx = {"model_bot": {"max": {"bracket": [{"bracket": "30°C"}, {"bracket": "31°C"}]}, "min": {"bracket": [{"bracket": "25°C"}]}},
           "model_bot_besok": {"tanggal": "2026-10-10", "max": {"bracket": [{"bracket": "31°C"}, {"bracket": "32°C"}]},
                               "min": {"bracket": [{"bracket": "26°C"}]}}}
    reply = {"max": {"perkiraan": 30.5, "peluang": {"30°C": 1, "31°C": 1}}, "min": {"perkiraan": 25.3, "peluang": {"25°C": 1}},
             "besok": {"max": {"perkiraan": 31.2, "peluang": {"31°C": 3, "32°C": 1}}, "min": {"peluang": {"26°C": 1}}},
             "per_jam": [{"jam": "15:00", "suhu": 30.1, "cuaca": "cerah"}, {"jam": "16:00", "suhu": "x"}],
             "ringkasan": "Panas.", "alasan": [], "risiko": []}
    monkeypatch.setattr(hk_ai, "gemini", lambda *a, **k: json.dumps(reply))
    view = hk_ai.ai_view(NOW, ctx)
    assert view["besok"]["max"] == {"point": 31.2, "probs": {"31°C": 0.75, "32°C": 0.25}}
    assert view["per_jam"] == [{"jam": "15:00", "suhu": 30.1, "cuaca": "cerah"}]
    today = {"max": {"mu": 30.4, "brackets": [{"bracket": "30°C", "model": 0.6, "market_prob": 0.5},
                                              {"bracket": "31°C", "model": 0.4, "market_prob": 0.5}]}}
    tomorrow = {"day": "2026-10-10", "max": {"mu": 31.0, "brackets": [{"bracket": "31°C", "model": 0.7, "market_prob": 0.6},
                                                                       {"bracket": "32°C", "model": 0.3, "market_prob": 0.4}]}}
    hk_ai.record_views(NOW, today, view, tomorrow)
    db = get_db_session()
    rows = {(r.local_date, r.kind, r.source) for r in db.query(HkForecastView).all()}
    db.close()
    assert ("2026-10-10", "max", "ai") in rows and ("2026-10-09", "hourly", "ai") in rows and ("2026-10-09", "max", "model") in rows
    latest = hk_ai.latest_views(NOW)
    assert latest["hourly"]["ai"]["probs"][0]["jam"] == "15:00"
    assert hk_ai.latest_views(NOW, day="2026-10-10")["max"]["ai"]["probs"]["31°C"] == 0.75


def test_hk_api_forecast_tomorrow_and_trades(http, monkeypatch):
    from fastapi.testclient import TestClient
    from app.dashboard import app
    monkeypatch.setattr("app.paper_trading.hko_alerts._today_market", lambda now, kind="highest", day=None: [])
    client = TestClient(app)
    data = client.get("/api/hk/forecast").json()
    assert data["now"]["text"] == "Cerah berawan" and len(data["nine_day"]["days"]) == 2
    tomorrow = client.get("/api/hk/bot", params={"day": "tomorrow"}).json()
    assert tomorrow["day"] == "tomorrow" and tomorrow["analysis"]["max"]["official_hint"] in (31, 29, None)
    assert client.get("/api/hk/bot", params={"day": "lusa"}).status_code == 400
    trades = client.get("/api/hk/trades").json()
    assert trades["stats"]["trades"] == 0 and trades["history"] == [] and trades["open"] == 0
    html = client.get("/hk").text
    assert 'id="fcHours"' in html and 'id="histRows"' in html and 'id="calGrid"' in html and 'id="aiHourly"' in html
    assert 'data-tab="insight"' not in html and 'id="insightText"' in html


def test_trade_history_supports_hk_all():
    from app.paper_trading import autotrader as at
    assert at.trade_history(strategy="hk_all") == []
