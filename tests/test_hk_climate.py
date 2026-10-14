"""Test klimatologi Hong Kong (data iklim HKO): parsing CSV, statistik bulan, insight, API & page /hk."""
from datetime import date, timedelta

import pytest

from app.paper_trading import hk_climate as hc

CSV = '''﻿"﻿日最高氣溫(攝氏度) - 天文台"
"Daily Maximum Temperature (°C) at the Hong Kong Observatory"
年/Year,月/Month,日/Day,數值/Value,"數據完整性/data Completeness"
2023,10,1,34.0,C
2023,10,2,***,
2023,10,3,33.1,#
"*** 沒有數據/unavailable"
'''


def test_parse_climate_csv_skips_missing_and_incomplete():
    assert hc.parse_climate_csv(CSV) == {date(2023, 10, 1): 34.0}


def synthetic(years=range(1990, 2026)):
    """Oktober: max 30°C, min 25°C; 2023 punya 3 hari ≥34, 2024 satu hari; 2025 malam hangat 28°C."""
    mx, mn = {}, {}
    for y in years:
        for d in range(1, 32):
            day = date(y, 10, d)
            mx[day], mn[day] = 30.0, 25.0
    for d in (1, 4, 5):
        mx[date(2023, 10, d)] = 34.2
    mx[date(2024, 10, 1)] = 34.0
    mn[date(2025, 10, 2)] = 28.1
    mx[date(1995, 10, 7)] = 35.1  # rekor
    return {"max": mx, "min": mn}


@pytest.fixture
def series(monkeypatch):
    data = synthetic()
    monkeypatch.setattr(hc, "official_series", lambda kind: data[kind])
    monkeypatch.setattr(hc, "recorded_daily", lambda after, today: {"max": {date(2026, 10, 1): 33.4},
                                                                     "min": {date(2026, 10, 1): 28.6}})
    return data


def test_month_report_counts_years_and_builds_insight(series):
    r = hc.month_report(10, years=10, max_threshold=34, min_threshold=28, today=date(2026, 10, 1))
    assert r["past_years"] == list(range(2016, 2026))
    assert r["hits"]["max"] == [{"year": 2023, "days": 3}, {"year": 2024, "days": 1}]
    assert r["thresholds"]["min_warm"] is True and r["hits"]["min"] == [{"year": 2025, "days": 1}]
    assert r["insight"].startswith("October has only hit a high of 34°C+ in 2 of the past 10 years: "
                                   "2023 (3 days) and 2024 (1 day).")
    assert "The October low has stayed at 28°C or warmer in 1 of the past 10 years: 2025 (1 day)." in r["insight"]
    assert r["insight"].endswith("Let's see what today brings. Gm HK🌄🌡")
    assert r["records"]["max"] == {"value": 35.1, "year": 1995, "date": "1995-10-07"}
    # hari ini dari bacaan real-time, ditandai sementara
    assert r["current"] == {"year": 2026, "max": 33.4, "min": 28.6, "days": 1, "provisional_days": 1}
    assert r["last_official"] == "2025-10-31"
    dist = {d["bracket"]: d for d in r["distribution"]["max"]}
    assert dist[30]["count"] > 0 and dist[34]["count"] == 4 and 35 not in dist  # 1995 di luar 30 tahun
    assert r["around_today"]["max"]["median"] == 30.0


def test_insight_when_threshold_never_hit(series):
    r = hc.month_report(10, years=10, max_threshold=36, min_threshold=None, today=date(2026, 10, 1))
    assert r["insight"].startswith("October hasn't hit a high of 36°C+ once in the past 10 years. "
                                   "The October record is 35.1°C (1995).")


def test_bracket_floor_and_default_thresholds():
    assert hc.bracket_of(33.9) == 33 and hc.bracket_of(34.0) == 34
    assert hc.default_thresholds({"estimate": 33.6, "min_estimate": 28.4}) == (33, 28)
    status = {"estimate": None, "official_hint": None, "min_estimate": None, "min_hint": None,
              "market": [{"bracket": "33°C", "price_yes": 0.6}, {"bracket": "34°C", "price_yes": 0.3}],
              "min_market": [{"bracket": "28°C", "price_yes": 0.7}]}
    assert hc.default_thresholds(status) == (33, 28)
    assert hc.default_thresholds(None) == (None, None)


def test_api_and_page(series, monkeypatch):
    from fastapi.testclient import TestClient
    from app.dashboard import app
    monkeypatch.setattr(hc, "live_summary", lambda: {"available": False})
    client = TestClient(app)
    data = client.get("/api/hk/climate", params={"month": 10, "max_threshold": 34, "min_threshold": 28}).json()
    assert data["month_name"] == "October" and data["hits"]["max"][0]["year"] in (2023, 2024, 2025)
    assert client.get("/api/hk/climate", params={"month": 13}).status_code == 400
    assert client.get("/api/hk/live").json() == {"available": False}
    html = client.get("/hk").text
    assert 'id="insightText"' in html and 'id="distMax"' in html and "/api/hk/climate" in html
    assert 'href="/hk"' in client.get("/").text


def test_hk_iklim_command(series, monkeypatch):
    from app.paper_trading.telegram_bot import handle_incoming_message
    monkeypatch.setattr(hc, "live_summary", lambda: {"available": True, "_status": {"estimate": 34.1, "min_estimate": 28.2}})
    text = handle_incoming_message("/hk iklim 10", sender_chat_id="1", allowed_chat_id="1")
    assert "Klimatologi October" in text and "hit a high of 34°C+" in text and "Gm HK" in text
