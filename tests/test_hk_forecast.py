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


# --- Ensemble, nowcast hujan, koreksi hujan, rezim --------------------------------------

NOWCAST_CSV = ("Updated Date and Time (in Hong Kong Time),Ending Date and Time (in Hong Kong Time),Latitude (degree),"
               "Longitude (degree),Half-hourly Nowcast Accumulated Rainfall (mm)\n"
               "202610091330,202610091400,22.304,114.163,0.4\n"
               "202610091330,202610091400,22.304,114.182,0.6\n"
               "202610091330,202610091400,22.380,114.230,3.5\n"
               "202610091330,202610091430,22.304,114.163,0.1\n"
               "202610091330,202610091400,23.487,112.956,9.9\n")


def ensemble_json():
    start = datetime(2026, 10, 9, 0, tzinfo=timezone.utc)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(72)]
    hourly = {"time": times}
    for model, peak in zip(hk_forecast.ENSEMBLE_MODELS, [31.0, 30.0, 30.5, 31.5, 29.0]):
        hourly[f"temperature_2m_{model}"] = [peak if (i + 8) % 24 == 14 else 27.0 for i in range(72)]
    return {"hourly": hourly}


@pytest.fixture
def wx(monkeypatch):
    state = {"warn": {}}

    def fake(url, data=None, timeout=10):
        if "Gridded_rainfall_nowcast" in url:
            return NOWCAST_CSV
        if "warnsum" in url:
            return json.dumps(state["warn"])
        if "models=" in url:
            return json.dumps(ensemble_json())
        if "rhrread" in url:
            return json.dumps(RHRREAD)
        if "open-meteo" in url:
            return json.dumps(open_meteo())
        if "dataType=fnd" in url:
            return json.dumps(FND)
        raise AssertionError(url)
    monkeypatch.setattr("app.paper_trading.live_market_data._http", fake)
    return state


def test_nowcast_and_rain_signal(wx):
    nc = hk_forecast.nowcast()
    assert nc["steps"][0]["near_mm"] == 0.5 and nc["steps"][0]["area_max_mm"] == 3.5   # titik jauh (9.9) diabaikan
    assert nc["near_total_mm"] == 0.6 and nc["updated"].startswith("2026-10-09T13:30")
    sig = hk_forecast.rain_signal(NOW)
    assert sig["expected"] and "nowcast 0.6 mm di stasiun" in sig["reasons"][0]
    wx["warn"] = {"WTS": {"name": "Thunderstorm Warning", "code": "WTS", "actionCode": "ISSUE"}}
    from app.paper_trading.live_market_data import clear_cache
    clear_cache()
    assert any("Badai petir" in r for r in hk_forecast.rain_signal(NOW)["reasons"])
    assert hk_forecast.regime(NOW)["name"] == "hujan"


def test_ensemble_extremes_and_table(wx):
    start = datetime(2026, 10, 10, 0, tzinfo=HKT)
    ext = hk_forecast.ensemble_extremes("highest", start, start + timedelta(days=1), None, None)
    assert {k: v["value"] for k, v in ext.items()} == dict(zip(hk_forecast.ENSEMBLE_MODELS, [31.0, 30.0, 30.5, 31.5, 29.0]))
    table = hk_forecast.ensemble_table(NOW)
    assert table[0]["name"] == "ECMWF" and table[0]["tomorrow_max"] == 31.0 and table[0]["tomorrow_min"] == 27.0


def test_estimate_blends_ensemble_and_cuts_rise_when_rain():
    now = datetime(2026, 10, 9, 11, 0, tzinfo=HKT).astimezone(timezone.utc)
    status = {"temp": 29.0, "max": 29.2, "min": 25.0, "official": {}}
    proj = [(datetime(2026, 10, 9, 14, tzinfo=HKT), 31.0)]
    ens = {"highest": {m: {"value": v} for m, v in zip("abcde", [31.0, 30.0, 30.5, 31.5, 29.0])}}
    est = hk_bot.estimate("highest", now, status, proj, {"ens": ens})
    assert est["ensemble_mean"] == pytest.approx(30.4) and est["mu"] == pytest.approx(30.7)   # (31 + 30.4) / 2
    assert est["spread"] == pytest.approx(0.86, abs=0.01) and est["sigma"] >= est["spread"]
    rainy = hk_bot.estimate("highest", now, status, proj, {"ens": ens, "rain": {"expected": True, "reasons": ["nowcast"]}})
    # sisa kenaikan dari 29.2 ke 30.7 (1.5) tinggal 40% → 29.8; ketidakpastian ×1.3
    assert rainy["mu"] == pytest.approx(29.8) and rainy["rain_adjusted"] and rainy["sigma"] == pytest.approx(est["sigma"] * 1.3, abs=0.01)
    warm = {"temp": 28.0, "max": 30.0, "min": 27.6, "official": {}}
    trough = [(datetime(2026, 10, 9, 23, tzinfo=HKT), 27.5)]
    dry = hk_bot.estimate("lowest", now, warm, trough, {})
    wet = hk_bot.estimate("lowest", now, warm, trough, {"rain": {"expected": True, "reasons": ["petir"]}})
    assert dry["mu"] == pytest.approx(27.5) and wet["mu"] == pytest.approx(27.0) and wet["rain_adjusted"]  # 28 − 1°C


def test_regime_calibration_adjusts_cloudy_days(monkeypatch):
    from decimal import Decimal
    from app.paper_trading import hk_calibration as hc
    from app.paper_trading.models import AutotradeSignal
    monkeypatch.setattr(hc, "_cache", {"at": 0.0, "value": None})
    monkeypatch.setattr("app.paper_trading.hk_ai.actual_extremes", lambda day: {"max": 30.0, "min": 25.0})
    today = datetime(2026, 10, 20, 1, 0, tzinfo=HKT)
    db = get_db_session()
    for i in range(20):
        day = today.date() - timedelta(days=i + 1)
        cloudy = i % 2 == 0           # hari mendung: proyeksi 31 (terlalu tinggi 1°C); cerah: proyeksi tepat 30
        at = datetime.combine(day, datetime.min.time(), tzinfo=HKT) + timedelta(hours=10)
        db.add(AutotradeSignal(signal_key=f"hk_max|{day}", strategy="hk_max", market_id=f"0x{i}", side="YES",
                               model_prob=Decimal("0.5"), price=Decimal("0.5"), fee=Decimal("0"), edge=Decimal("0"),
                               action="skipped", local_day=day.isoformat(), created_at=at.astimezone(timezone.utc),
                               features=json.dumps({"mu_raw": 31.0 if cloudy else 30.0, "observed": 28.0, "hour_hkt": 10,
                                                    "regime": "mendung" if cloudy else "cerah"})))
    db.commit()
    db.close()
    cal = hc.calibrate(today.astimezone(timezone.utc))
    assert cal["bias"]["max"]["09–12"]["mean"] == pytest.approx(-0.5)
    mendung, cerah = cal["regime"]["max"]["mendung"], cal["regime"]["max"]["cerah"]
    assert mendung["days"] == 10 and mendung["mean"] == pytest.approx(-0.5) and cerah["mean"] == pytest.approx(0.5)
    assert mendung["value"] == pytest.approx(-0.3)   # langkah maks per hari
    assert hc.bias_for("max", 10.5, "mendung") == pytest.approx(cal["bias"]["max"]["09–12"]["value"] - 0.3)


def test_forecast_api_has_nowcast_and_ensemble(wx):
    from fastapi.testclient import TestClient
    from app.dashboard import app
    data = TestClient(app).get("/api/hk/forecast").json()
    assert data["rain"]["expected"] and data["regime"]["name"] == "hujan" and len(data["ensemble"]) == 5
    assert data["nowcast"]["steps"][0]["near_mm"] == 0.5


def test_hk_side_groups_and_trades_api():
    from decimal import Decimal
    from app.paper_trading import autotrader as at
    from app.paper_trading.models import AutotradeDecision
    assert at._versions_for("hk_yes") == ["auto_hk_max_v1", "auto_hk_min_v1"]
    assert at._versions_for("hk_no") == ["auto_hk_max_no_v1", "auto_hk_min_no_v1"]
    db = get_db_session()
    for i, strat in enumerate(("hk_max", "hk_max_no", "hk_min_no")):
        db.add(AutotradeDecision(decision_key=f"{strat}|d", strategy=strat, market_id=f"0x{i}", label=f"HK · {strat}",
                                 side="NO" if strat.endswith("_no") else "YES", model_prob=Decimal("0.9"), price=Decimal("0.9"),
                                 fee=Decimal("0"), edge=Decimal("0.05"), size_usd=Decimal("1"), status="filled",
                                 local_day="2026-10-10", created_at=NOW))
    db.commit()
    db.close()
    assert {r["strategy"] for r in at.trade_history(strategy="hk_no")} == {"hk_max_no", "hk_min_no"}
    assert {r["strategy"] for r in at.trade_history(strategy="hk_yes")} == {"hk_max"}
    from fastapi.testclient import TestClient
    from app.dashboard import app
    client = TestClient(app)
    data = client.get("/api/hk/trades", params={"side": "no"}).json()
    assert data["side"] == "no" and len(data["history"]) == 2 and set(data["by_side"]) == {"yes", "no"}
    assert client.get("/api/hk/trades", params={"side": "maybe"}).status_code == 400
    assert client.get("/api/autotrade/calendar", params={"strategy": "hk_no"}).status_code == 200
    html = client.get("/hk").text
    assert 'id="histSide"' in html and 'id="calSide"' in html and 'id="histSides"' in html
