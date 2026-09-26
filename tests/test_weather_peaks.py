"""
Test rekomendasi market suhu berbasis jam puncak lokal (app/paper_trading/weather_peaks.py).
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.core.database import get_db_session
from app.paper_trading.models import MarketSnapshot
from app.paper_trading.weather_peaks import (
    CITY_TIMEZONES,
    filter_peak_time_suggestions,
    parse_temperature_market,
    recommendation_window,
    upcoming_recommendation_windows,
)

UTC = timezone.utc
# 51 kota yang tercatat di market suhu Polymarket (pertanyaan market, September 2026)
LIVE_CITIES = [
    "Amsterdam", "Ankara", "Atlanta", "Austin", "Beijing", "Buenos Aires", "Busan", "Cape Town", "Chengdu",
    "Chicago", "Chongqing", "Dallas", "Denver", "Guangzhou", "Helsinki", "Hong Kong", "Houston", "Istanbul",
    "Jeddah", "Jinan", "Karachi", "Kuala Lumpur", "London", "Los Angeles", "Lucknow", "Madrid", "Manila",
    "Mexico City", "Miami", "Milan", "Moscow", "Munich", "New York City", "Panama City", "Paris", "Qingdao",
    "San Francisco", "Sao Paulo", "Seattle", "Seoul (Incheon)", "Shanghai", "Shenzhen", "Singapore", "Taipei",
    "Tel Aviv", "Tokyo", "Toronto", "Warsaw", "Wellington", "Wuhan", "Zhengzhou",
]


def market(name, price_yes="0.72", status="open", market_id=None, end_date=None):
    py = Decimal(price_yes)
    return {
        "market_id": market_id or f"0x{abs(hash(name)) % 10**8}", "market_name": name, "status": status,
        "is_resolved": status == "resolved", "price_yes": py, "price_no": Decimal("1") - py,
        "end_date": end_date, "resolution_time": end_date,
    }


HK_HIGH = "Will the highest temperature in Hong Kong be 31°C or higher on September 26?"
LA_LOW = "Will the lowest temperature in Los Angeles be between 60-61°F on September 26?"
END_SEP26 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


class TestParsing:

    def test_parse_highest_question(self):
        p = parse_temperature_market(HK_HIGH, END_SEP26)
        assert (p.kind, p.city, p.local_date) == ("highest", "Hong Kong", date(2026, 9, 26))

    def test_parse_lowest_question_with_between(self):
        p = parse_temperature_market(LA_LOW, END_SEP26)
        assert (p.kind, p.city) == ("lowest", "Los Angeles")

    @pytest.mark.parametrize("name,city", [
        ("Will the highest temperature in New York City be 57°F or below on September 25?", "New York City"),
        ("Will the highest temperature in Seoul (Incheon) be 22°C on September 25?", "Seoul (Incheon)"),
        ("Highest temperature in NYC on September 25?", "NYC"),
    ])
    def test_parse_city_variants(self, name, city):
        assert parse_temperature_market(name, END_SEP26).city == city

    def test_year_inferred_without_end_date(self):
        p = parse_temperature_market(HK_HIGH, None, now=datetime(2026, 9, 20, tzinfo=UTC))
        assert p.local_date == date(2026, 9, 26)

    def test_non_temperature_market_ignored(self):
        assert parse_temperature_market("Will Elon Musk post 200-219 tweets?") is None

    def test_all_live_cities_have_valid_timezones(self):
        for city in LIVE_CITIES:
            assert city in CITY_TIMEZONES, city
            ZoneInfo(CITY_TIMEZONES[city])


class TestWindows:

    def test_default_highest_window_is_12_to_13_local(self):
        w = recommendation_window("Hong Kong", "highest", date(2026, 9, 26))
        assert (w.start.hour, w.end.hour, w.peak_start.hour, w.peak_end.hour) == (12, 13, 14, 15)
        assert w.start.astimezone(UTC) == datetime(2026, 9, 26, 4, 0, tzinfo=UTC)  # HKT = UTC+8

    def test_default_lowest_window_is_03_to_04_local(self):
        w = recommendation_window("Los Angeles", "lowest", date(2026, 9, 26))
        assert (w.start.hour, w.end.hour) == (3, 4)
        assert w.start.astimezone(UTC) == datetime(2026, 9, 26, 10, 0, tzinfo=UTC)  # PDT = UTC-7

    def test_daylight_saving_is_respected(self):
        summer = recommendation_window("London", "highest", date(2026, 7, 1))   # BST
        winter = recommendation_window("London", "highest", date(2026, 12, 1))  # GMT
        assert summer.start.astimezone(UTC).hour == 11
        assert winter.start.astimezone(UTC).hour == 12

    def test_configurable_lead_and_window(self, monkeypatch):
        monkeypatch.setattr(settings, "RECOMMENDATION_LEAD_HOURS", 2.0)
        monkeypatch.setattr(settings, "RECOMMENDATION_WINDOW_HOURS", 0.5)
        w = recommendation_window("Tokyo", "highest", date(2026, 9, 26))
        assert (f"{w.start:%H:%M}", f"{w.end:%H:%M}") == ("11:30", "12:00")

    def test_per_city_peak_override(self, monkeypatch):
        monkeypatch.setattr(settings, "TEMP_PEAK_HOUR_OVERRIDES", '{"Hong Kong": {"highest": 15}}')
        w = recommendation_window("Hong Kong", "highest", date(2026, 9, 26))
        assert (w.start.hour, w.peak_start.hour) == (13, 15)
        assert recommendation_window("Tokyo", "highest", date(2026, 9, 26)).peak_start.hour == 14

    def test_unknown_city_skipped_unless_overridden(self, monkeypatch):
        assert recommendation_window("Lima", "highest", date(2026, 9, 26)) is None
        monkeypatch.setattr(settings, "CITY_TIMEZONE_OVERRIDES", '{"Lima": "America/Lima"}')
        assert recommendation_window("Lima", "highest", date(2026, 9, 26)).start.hour == 12

    def test_invalid_override_json_is_ignored(self, monkeypatch):
        monkeypatch.setattr(settings, "TEMP_PEAK_HOUR_OVERRIDES", "{not json")
        assert recommendation_window("Hong Kong", "highest", date(2026, 9, 26)).start.hour == 12


class TestSuggestions:
    IN_HK_WINDOW = datetime(2026, 9, 26, 4, 30, tzinfo=UTC)  # 12:30 HKT

    def test_recommended_inside_local_window(self):
        res = filter_peak_time_suggestions([market(HK_HIGH, end_date=END_SEP26)], now=self.IN_HK_WINDOW)
        assert len(res) == 1
        r = res[0]
        assert (r["city"], r["kind"], r["side"], r["current_price"]) == ("Hong Kong", "highest", "YES", 0.72)
        assert r["time_remaining"] == "0h 30m"
        assert r["local_time"] == "12:30 HKT"
        assert "12:00–13:00" in r["window_label"] and "puncak 14:00–15:00" in r["window_label"]

    @pytest.mark.parametrize("now", [
        datetime(2026, 9, 26, 3, 59, tzinfo=UTC),   # 11:59 HKT (belum mulai)
        datetime(2026, 9, 26, 5, 0, tzinfo=UTC),    # 13:00 HKT (sudah lewat)
        datetime(2026, 9, 25, 4, 30, tzinfo=UTC),   # jam yang sama, tapi tanggal lokal lain
    ])
    def test_not_recommended_outside_window(self, now):
        assert filter_peak_time_suggestions([market(HK_HIGH, end_date=END_SEP26)], now=now) == []

    def test_lowest_market_uses_lowest_window(self):
        m = market(LA_LOW, end_date=END_SEP26)
        assert filter_peak_time_suggestions([m], now=datetime(2026, 9, 26, 10, 15, tzinfo=UTC))  # 03:15 PDT
        assert not filter_peak_time_suggestions([m], now=datetime(2026, 9, 26, 19, 15, tzinfo=UTC))  # 12:15 PDT

    def test_price_filter_picks_yes_then_no(self):
        yes = market(HK_HIGH, price_yes="0.73", market_id="0xyes", end_date=END_SEP26)
        no = market(HK_HIGH.replace("31", "33"), price_yes="0.27", market_id="0xno", end_date=END_SEP26)
        out = market(HK_HIGH.replace("31", "29"), price_yes="0.50", market_id="0xout", end_date=END_SEP26)
        res = {r["market_id"]: r["side"] for r in filter_peak_time_suggestions([yes, no, out], now=self.IN_HK_WINDOW)}
        assert res == {"0xyes": "YES", "0xno": "NO"}

    def test_custom_price_range(self):
        m = market(HK_HIGH, price_yes="0.50", end_date=END_SEP26)
        assert filter_peak_time_suggestions([m], min_price=0.45, max_price=0.55, now=self.IN_HK_WINDOW)

    def test_markets_not_accepting_orders_excluded(self):
        for status in ("closed", "resolved"):
            m = market(HK_HIGH, status=status, end_date=END_SEP26)
            assert filter_peak_time_suggestions([m], now=self.IN_HK_WINDOW) == []

    def test_open_market_after_end_date_still_recommended_for_americas(self):
        """endDate 12:00 UTC sudah lewat saat jendela LA (19:00 UTC) — market tetap direkomendasikan."""
        m = market("Will the highest temperature in Los Angeles be 76-77°F on September 26?", end_date=END_SEP26)
        assert filter_peak_time_suggestions([m], now=datetime(2026, 9, 26, 19, 30, tzinfo=UTC))  # 12:30 PDT


class TestSchedule:

    def test_upcoming_windows_sorted_with_active_flag(self):
        now = datetime(2026, 9, 26, 4, 30, tzinfo=UTC)
        markets = [
            market(HK_HIGH, end_date=END_SEP26),
            market(HK_HIGH.replace("31", "32"), end_date=END_SEP26),
            market("Will the highest temperature in Paris be 22°C on September 26?", end_date=END_SEP26),
            market(LA_LOW, end_date=END_SEP26),
        ]
        sched = upcoming_recommendation_windows(markets, now=now)
        assert [(w["city"], w["kind"]) for w in sched] == [
            ("Hong Kong", "highest"), ("Paris", "highest"), ("Los Angeles", "lowest")]
        assert sched[0]["active"] is True and sched[0]["markets"] == 2
        assert sched[0]["starts_in"] == "sedang berlangsung"
        assert sched[1]["starts_in"] == "5h 30m"  # Paris 12:00 CEST = 10:00 UTC

    def test_suggestions_sorted_by_absolute_time_across_timezones(self):
        # 03:30 UTC: Tokyo (12:30 JST, jendela berakhir 04:00 UTC) & Hong Kong (11:30 HKT, belum mulai)
        # 04:30 UTC: Hong Kong aktif (berakhir 05:00 UTC); tambah Manila (UTC+8) agar dua kota sama-sama aktif
        now = datetime(2026, 9, 26, 4, 30, tzinfo=UTC)
        markets = [
            market("Will the highest temperature in Manila be 33°C on September 26?", market_id="0xmnl", end_date=END_SEP26),
            market(HK_HIGH, market_id="0xhk", end_date=END_SEP26),
            market("Will the lowest temperature in Wellington be 8°C on September 27?", market_id="0xwlg",
                   end_date=datetime(2026, 9, 27, 12, 0, tzinfo=UTC)),  # 16:30 NZST, di luar jendela
        ]
        res = filter_peak_time_suggestions(markets, now=now)
        assert [r["city"] for r in res] == ["Hong Kong", "Manila"]

    def test_past_windows_excluded(self):
        now = datetime(2026, 9, 26, 23, 0, tzinfo=UTC)
        assert upcoming_recommendation_windows([market(HK_HIGH, end_date=END_SEP26)], now=now) == []


class TestServiceAndApi:
    NOW = datetime(2026, 9, 26, 4, 30, tzinfo=UTC)

    def _store(self, name, price):
        db = get_db_session()
        db.add(MarketSnapshot(id=uuid.uuid4(), market_id="0xhk31", market_name=name, status="open", is_resolved=False,
                              resolution_time=END_SEP26, end_date=END_SEP26, price_yes=Decimal(price),
                              price_no=Decimal("1") - Decimal(price), category="Temperature", timestamp=self.NOW))
        db.commit()
        db.close()

    def test_service_reads_markets_from_database(self):
        from app.paper_service import get_market_suggestions
        self._store(HK_HIGH, "0.74")
        res = get_market_suggestions(now=self.NOW)
        assert [r["market_id"] for r in res] == ["0xhk31"]
        assert res[0]["polymarket_url"].startswith("https://polymarket.com/")

    def test_api_endpoints(self):
        from app.dashboard import get_market_suggestions_api, get_recommendation_schedule_api
        self._store(HK_HIGH, "0.74")
        with patch("app.paper_trading.weather_peaks._utcnow", return_value=self.NOW):
            assert [r["city"] for r in get_market_suggestions_api()] == ["Hong Kong"]
            assert get_recommendation_schedule_api()[0]["active"] is True
