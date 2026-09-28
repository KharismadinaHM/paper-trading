"""
Test rekomendasi market suhu berbasis jam puncak lokal tiap kota (app/paper_trading/weather_peaks.py).
"""
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import app.paper_trading.weather_peaks as wp
from app.core.config import settings
from app.core.database import get_db_session
from app.paper_trading.cities import CITIES
from app.paper_trading.models import MarketSnapshot
from app.paper_trading.solar import solar_noon_utc, sunrise_utc
from app.paper_trading.weather_peaks import (
    CITY_TIMEZONES,
    bracket_label,
    filter_peak_time_suggestions,
    parse_temperature_market,
    peak_center,
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

# Kalibrasi deterministik untuk test (file riset asli diuji terpisah).
# Hong Kong: solar noon 26 Sep ≈ 12:14 HKT, lag 2.27 jam → pusat puncak 14:30 → puncak 14:00–15:00.
TEST_CALIBRATION = {
    "Hong Kong": {"lag_max_hours": 2.27, "lag_min_hours": 0.3},
    "Los Angeles": {"lag_max_hours": 1.0, "lag_min_hours": 0.0},
    "Madrid": {"lag_max_hours": 2.72, "lag_min_hours": 0.34},
    "Shanghai": {"lag_max_hours": 1.0, "lag_min_hours": 0.0},
    "Chengdu": {"lag_max_hours": 1.0, "lag_min_hours": 0.0},
}


@pytest.fixture(autouse=True)
def fixed_calibration(monkeypatch):
    monkeypatch.setattr(wp, "load_calibration", lambda: TEST_CALIBRATION)


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
D = date(2026, 9, 26)


class TestSolar:

    @pytest.mark.parametrize("city,expected", [
        # Sunrise 26 Sep 2026 menurut Open-Meteo (waktu lokal)
        ("New York City", "06:47"), ("Madrid", "08:05"), ("Helsinki", "07:13"),
        ("Hong Kong", "06:13"), ("Sydney", "05:40"),
    ])
    def test_sunrise_matches_reference_within_3_minutes(self, city, expected):
        c = CITIES[city]
        ours = sunrise_utc(D, c.lat, c.lon).astimezone(ZoneInfo(c.tz))
        ref = datetime.combine(D, datetime.strptime(expected, "%H:%M").time(), tzinfo=ZoneInfo(c.tz))
        assert abs((ours - ref).total_seconds()) <= 180

    def test_solar_noon_clock_time_differs_by_longitude_within_same_timezone(self):
        """China satu zona waktu: solar noon Chengdu ~1 jam lebih lambat dari Shanghai."""
        tz = ZoneInfo("Asia/Shanghai")
        chengdu = solar_noon_utc(D, CITIES["Chengdu"].lon).astimezone(tz)
        shanghai = solar_noon_utc(D, CITIES["Shanghai"].lon).astimezone(tz)
        assert timedelta(minutes=60) <= chengdu - shanghai <= timedelta(minutes=80)


class TestParsing:

    def test_parse_highest_question(self):
        p = parse_temperature_market(HK_HIGH, END_SEP26)
        assert (p.kind, p.city, p.local_date) == ("highest", "Hong Kong", D)

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

    @pytest.mark.parametrize("name,bracket", [
        (HK_HIGH, "31°C or higher"), (LA_LOW, "60-61°F"),
        ("Will the lowest temperature in Mexico City be 8°C on September 26?", "8°C"),
    ])
    def test_bracket_label(self, name, bracket):
        assert bracket_label(name) == bracket

    def test_year_inferred_without_end_date(self):
        assert parse_temperature_market(HK_HIGH, None, now=datetime(2026, 9, 20, tzinfo=UTC)).local_date == D

    def test_non_temperature_market_ignored(self):
        assert parse_temperature_market("Will Elon Musk post 200-219 tweets?") is None

    def test_all_live_cities_have_timezone_and_coordinates(self):
        for city in LIVE_CITIES:
            assert city in CITIES, city
            ZoneInfo(CITY_TIMEZONES[city])
        assert CITY_TIMEZONES["NYC"] == "America/New_York"


class TestPeakTimes:

    def test_highest_peak_from_solar_noon_plus_city_lag(self):
        w = recommendation_window("Hong Kong", "highest", D)
        assert (f"{w.peak_start:%H:%M}", f"{w.peak_end:%H:%M}") == ("14:00", "15:00")
        assert (f"{w.start:%H:%M}", f"{w.end:%H:%M}") == ("12:00", "13:00")
        assert w.source == "data"

    def test_lowest_peak_from_sunrise_plus_city_lag(self):
        c = CITIES["Hong Kong"]
        rise = sunrise_utc(D, c.lat, c.lon).astimezone(ZoneInfo(c.tz))  # ~06:12
        center, source = peak_center("Hong Kong", "lowest", D)
        assert source == "data"
        assert abs((center - (rise + timedelta(hours=0.3))).total_seconds()) <= 8 * 60  # dibulatkan ke 15 menit

    def test_cities_peak_at_different_clock_times(self):
        """Madrid (zona waktu 'kecepatan' + DST) puncaknya jauh lebih sore dibanding Hong Kong."""
        madrid = recommendation_window("Madrid", "highest", D)
        hk = recommendation_window("Hong Kong", "highest", D)
        assert madrid.peak_start.hour >= 16 and hk.peak_start.hour == 14

    def test_same_timezone_different_longitude_different_peak(self):
        chengdu = recommendation_window("Chengdu", "highest", D).peak_start
        shanghai = recommendation_window("Shanghai", "highest", D).peak_start
        assert chengdu - shanghai >= timedelta(minutes=60)

    def test_daylight_saving_shifts_clock_time(self):
        summer = recommendation_window("Madrid", "highest", date(2026, 7, 1)).peak_start
        winter = recommendation_window("Madrid", "highest", date(2026, 12, 1)).peak_start
        # CEST → CET: jam dinding puncak musim dingin ~1 jam lebih awal (± equation of time)
        diff = (summer.hour * 60 + summer.minute) - (winter.hour * 60 + winter.minute)
        assert 30 <= diff <= 90

    def test_city_without_calibration_uses_model_default(self):
        w = recommendation_window("Tokyo", "highest", D)  # tidak ada di TEST_CALIBRATION
        noon = solar_noon_utc(D, CITIES["Tokyo"].lon).astimezone(ZoneInfo("Asia/Tokyo"))
        center = w.peak_start + timedelta(minutes=30)
        assert w.source == "model"
        assert abs((center - (noon + timedelta(hours=2))).total_seconds()) <= 8 * 60

    def test_override_sets_peak_start_hour(self, monkeypatch):
        monkeypatch.setattr(settings, "TEMP_PEAK_HOUR_OVERRIDES", '{"Hong Kong": {"highest": 15}}')
        w = recommendation_window("Hong Kong", "highest", D)
        assert (w.peak_start.hour, w.start.hour, w.source) == (15, 13, "override")

    def test_invalid_override_json_is_ignored(self, monkeypatch):
        monkeypatch.setattr(settings, "TEMP_PEAK_HOUR_OVERRIDES", "{not json")
        assert recommendation_window("Hong Kong", "highest", D).peak_start.hour == 14

    def test_configurable_lead_and_window(self, monkeypatch):
        monkeypatch.setattr(settings, "RECOMMENDATION_LEAD_HOURS", 2.0)
        monkeypatch.setattr(settings, "RECOMMENDATION_WINDOW_HOURS", 0.5)
        w = recommendation_window("Hong Kong", "highest", D)
        assert (f"{w.start:%H:%M}", f"{w.end:%H:%M}") == ("11:30", "12:00")

    def test_unknown_city_skipped_unless_overridden(self, monkeypatch):
        assert recommendation_window("Lima", "highest", D) is None
        monkeypatch.setattr(settings, "CITY_TIMEZONE_OVERRIDES", '{"Lima": "America/Lima"}')
        w = recommendation_window("Lima", "highest", D)
        assert (w.peak_start.hour, w.source) == (14, "model")  # jam cadangan TEMP_HIGH_PEAK_HOUR


class TestSuggestions:
    IN_HK_WINDOW = datetime(2026, 9, 26, 4, 30, tzinfo=UTC)  # 12:30 HKT

    def test_event_recommended_inside_local_window_without_price_filter(self):
        brackets = [market(HK_HIGH.replace("31", str(t)), price_yes=p, market_id=f"0x{t}", end_date=END_SEP26)
                    for t, p in ((29, "0.10"), (30, "0.55"), (31, "0.30"), (32, "0.003"))]
        res = filter_peak_time_suggestions(brackets, now=self.IN_HK_WINDOW)
        assert len(res) == 1
        ev = res[0]
        assert (ev["city"], ev["kind"], ev["market_count"]) == ("Hong Kong", "highest", 4)
        assert [m["market_id"] for m in ev["markets"]] == ["0x30", "0x31", "0x29", "0x32"]  # peluang tertinggi dulu
        assert ev["markets"][0]["bracket"] == "30°C or higher"
        assert ev["time_remaining"] == "0h 30m" and ev["local_time"] == "12:30 HKT"
        assert "12:00–13:00" in ev["window_label"] and "puncak 14:00–15:00" in ev["window_label"]

    @pytest.mark.parametrize("now", [
        datetime(2026, 9, 26, 3, 59, tzinfo=UTC),   # 11:59 HKT (belum mulai)
        datetime(2026, 9, 26, 5, 0, tzinfo=UTC),    # 13:00 HKT (sudah lewat)
        datetime(2026, 9, 25, 4, 30, tzinfo=UTC),   # jam yang sama, tanggal lokal lain
    ])
    def test_not_recommended_outside_window(self, now):
        assert filter_peak_time_suggestions([market(HK_HIGH, end_date=END_SEP26)], now=now) == []

    def test_lowest_market_uses_sunrise_based_window(self):
        m = market(LA_LOW, end_date=END_SEP26)
        w = recommendation_window("Los Angeles", "lowest", D)
        inside = w.start.astimezone(UTC) + timedelta(minutes=10)
        assert filter_peak_time_suggestions([m], now=inside)
        assert not filter_peak_time_suggestions([m], now=w.end.astimezone(UTC) + timedelta(minutes=1))

    def test_optional_price_filter(self):
        m = market(HK_HIGH, price_yes="0.50", end_date=END_SEP26)
        assert filter_peak_time_suggestions([m], min_price=0.45, max_price=0.55, now=self.IN_HK_WINDOW)
        assert not filter_peak_time_suggestions([m], min_price=0.70, max_price=0.75, now=self.IN_HK_WINDOW)

    def test_markets_not_accepting_orders_excluded(self):
        for status in ("closed", "resolved"):
            assert filter_peak_time_suggestions([market(HK_HIGH, status=status, end_date=END_SEP26)],
                                                now=self.IN_HK_WINDOW) == []

    def test_open_market_after_end_date_still_recommended_for_americas(self):
        """endDate 12:00 UTC sudah lewat saat jendela LA — market yang masih open tetap direkomendasikan."""
        m = market("Will the highest temperature in Los Angeles be 76-77°F on September 26?", end_date=END_SEP26)
        w = recommendation_window("Los Angeles", "highest", D)
        now = w.start.astimezone(UTC) + timedelta(minutes=5)
        assert now > END_SEP26
        assert filter_peak_time_suggestions([m], now=now)

    def test_events_sorted_by_absolute_time_across_timezones(self, monkeypatch):
        # Lebarkan jendela agar Hong Kong (UTC+8) dan Madrid (UTC+2) sama-sama aktif
        monkeypatch.setattr(settings, "RECOMMENDATION_WINDOW_HOURS", 12.0)
        now = datetime(2026, 9, 26, 4, 0, tzinfo=UTC)  # HK s/d 05:00 UTC, Madrid s/d ~13:15 UTC
        markets = [market("Will the highest temperature in Madrid be 28°C on September 26?", end_date=END_SEP26),
                   market(HK_HIGH, end_date=END_SEP26)]
        res = filter_peak_time_suggestions(markets, now=now)
        assert [e["city"] for e in res] == ["Hong Kong", "Madrid"]
        ends = [datetime.fromisoformat(e["window_end"]) for e in res]
        assert ends == sorted(ends)


class TestSchedule:

    def test_upcoming_windows_sorted_with_active_flag(self):
        now = datetime(2026, 9, 26, 4, 30, tzinfo=UTC)  # HK aktif
        markets = [
            market(HK_HIGH, end_date=END_SEP26),
            market(HK_HIGH.replace("31", "32"), end_date=END_SEP26),
            market("Will the highest temperature in Madrid be 28°C on September 26?", end_date=END_SEP26),
            market(LA_LOW, end_date=END_SEP26),
        ]
        sched = upcoming_recommendation_windows(markets, now=now)
        assert {(w["city"], w["kind"]) for w in sched} == {
            ("Hong Kong", "highest"), ("Los Angeles", "lowest"), ("Madrid", "highest")}
        assert sched[0]["city"] == "Hong Kong" and sched[0]["active"] is True and sched[0]["markets"] == 2
        assert sched[0]["starts_in"] == "sedang berlangsung"
        starts = [datetime.fromisoformat(w["starts_at"]) for w in sched]
        assert starts == sorted(starts)

    def test_past_windows_excluded(self):
        now = datetime(2026, 9, 26, 23, 0, tzinfo=UTC)
        assert upcoming_recommendation_windows([market(HK_HIGH, end_date=END_SEP26)], now=now) == []


class TestCalibrationFile:
    """Validasi file hasil riset yang dipakai aplikasi (peak_calibration.json)."""

    def test_calibration_covers_all_live_cities_with_plausible_lags(self):
        data = json.loads(wp.CALIBRATION_FILE.read_text())
        cities = data["cities"]
        for city in LIVE_CITIES:
            c = cities[city]
            assert c["days"] >= 60, city
            assert -2.0 <= c["lag_max_hours"] <= 5.0, city   # max: sekitar/sesudah solar noon
            # min: umumnya sekitar matahari terbit; kota pesisir bisa beberapa jam sebelumnya (mis. Istanbul ~03:00)
            assert -4.0 <= c["lag_min_hours"] <= 2.0, city
        assert data["source"].startswith("Open-Meteo")


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
        self._store(HK_HIGH, "0.12")
        res = get_market_suggestions(now=self.NOW)
        assert [e["city"] for e in res] == ["Hong Kong"]
        assert res[0]["markets"][0]["market_id"] == "0xhk31"
        assert res[0]["markets"][0]["polymarket_url"].startswith("https://polymarket.com/")

    def test_api_endpoints(self):
        from app.dashboard import get_market_suggestions_api, get_recommendation_schedule_api
        self._store(HK_HIGH, "0.12")
        with patch("app.paper_trading.weather_peaks._utcnow", return_value=self.NOW):
            assert [e["city"] for e in get_market_suggestions_api()] == ["Hong Kong"]
            assert get_recommendation_schedule_api()[0]["active"] is True


class TestTopCitiesByVolume:

    def test_ranks_cities_by_total_volume_of_open_markets(self):
        markets = [
            {**market(HK_HIGH, market_id="0x1", end_date=END_SEP26), "volume": 100},
            {**market(HK_HIGH.replace("31°C or higher", "30°C"), market_id="0x2", end_date=END_SEP26), "volume": 100},
            {**market(LA_LOW, market_id="0x3", end_date=END_SEP26), "volume": 150},
            {**market(LA_LOW.replace("60-61", "62-63"), market_id="0x4", status="resolved", end_date=END_SEP26),
             "volume": 10_000},  # resolved tidak dihitung
        ]
        now = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
        assert wp.top_cities_by_volume(markets, limit=7, now=now) == ["Hong Kong", "Los Angeles"]
        assert wp.top_cities_by_volume(markets, limit=1, now=now) == ["Hong Kong"]

    def test_none_without_volume_data(self):
        now = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
        assert wp.top_cities_by_volume([market(HK_HIGH, end_date=END_SEP26)], limit=7, now=now) is None


class TestCityVolumeSummary:

    def test_splits_volume_by_kind(self):
        markets = [
            {**market(HK_HIGH, market_id="0x1", end_date=END_SEP26), "volume": 100},
            {**market(HK_HIGH.replace("highest", "lowest"), market_id="0x2", end_date=END_SEP26), "volume": 40},
            {**market(LA_LOW, market_id="0x3", end_date=END_SEP26), "volume": 30},
        ]
        rows = wp.city_volume_summary(markets, now=datetime(2026, 9, 26, 0, 0, tzinfo=UTC))
        assert [(r["city"], r["volume"], r["highest"], r["lowest"], r["market_count"]) for r in rows] == [
            ("Hong Kong", 140, 100, 40, 2), ("Los Angeles", 30, 0, 30, 1)]

    def test_next_window_rolls_to_tomorrow_after_today_passed(self):
        # Hong Kong highest: jendela 12:00–13:00 HKT; pukul 20:00 HKT → jendela besok
        w = wp.next_recommendation_window("Hong Kong", "highest", now=datetime(2026, 9, 26, 12, 0, tzinfo=UTC))
        assert w.start.date() == date(2026, 9, 27)
        w = wp.next_recommendation_window("Hong Kong", "highest", now=datetime(2026, 9, 26, 1, 0, tzinfo=UTC))
        assert w.start.date() == date(2026, 9, 26)


class TestRecommendationKinds:

    def test_only_enabled_kinds_are_recommended(self, monkeypatch):
        monkeypatch.setattr(settings, "RECOMMENDATION_KINDS", "highest")
        lows = "Will the lowest temperature in Hong Kong be 26°C on September 26?"
        markets = [market(HK_HIGH, market_id="0x1", end_date=END_SEP26),
                   market(lows, market_id="0x2", end_date=END_SEP26)]
        assert {w["kind"] for w in wp.upcoming_recommendation_windows(
            markets, now=datetime(2026, 9, 25, 16, 0, tzinfo=UTC))} == {"highest"}

    @pytest.mark.parametrize("value,expected", [
        ("highest", {"highest"}), ("Lowest, highest", {"highest", "lowest"}), ("", {"highest", "lowest"}),
        ("bogus", {"highest", "lowest"}),
    ])
    def test_kinds_setting_parsing(self, monkeypatch, value, expected):
        monkeypatch.setattr(settings, "RECOMMENDATION_KINDS", value)
        assert wp.recommendation_kinds() == expected
