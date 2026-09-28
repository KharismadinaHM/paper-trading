"""
Test kondisi cuaca (METAR/HKO), tren suhu per jam, perkiraan max/min, dan kalimat kesimpulan.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.paper_trading import weather_outlook as wo

UTC = timezone.utc
SEOUL = ZoneInfo("Asia/Seoul")
D = date(2026, 9, 28)


def at(hour, minute=0):
    """Jam lokal Seoul pada tanggal D (aware, UTC)."""
    return datetime(2026, 9, 28, hour, minute, tzinfo=SEOUL).astimezone(UTC)


class TestConditions:

    @pytest.mark.parametrize("report,label,emoji,dim", [
        ({"cover": "CLR"}, "Cerah", "☀️", False),
        ({"cover": "FEW"}, "Cerah berawan", "🌤️", False),
        ({"cover": "OVC", "wxString": "-RA BR"}, "Mendung, hujan ringan, berkabut", "🌧️", True),
        ({"cover": "BKN", "wxString": "+TSRA"}, "Berawan, badai petir lebat, hujan", "⛈️", True),
        ({"cover": "SCT", "wxString": "HZ"}, "Berawan sebagian, berkabut asap", "⛅", False),
        ({"wxString": "FG"}, "Kabut tebal", "🌫️", False),
    ])
    def test_metar(self, report, label, emoji, dim):
        c = wo.condition_from_metar(report)
        assert (c["label"], c["emoji"], c["dim"]) == (label, emoji, dim)

    def test_hko_icon(self):
        assert wo.condition_from_hko(61)["label"] == "Mendung"
        assert wo.condition_from_hko(None) is None and wo.condition_from_hko(999) is None

    @pytest.mark.parametrize("temp,label", [(36, "Sangat panas"), (31, "Panas"), (25, "Hangat"), (18, "Sejuk"),
                                            (10, "Dingin"), (2, "Sangat dingin"), (None, None)])
    def test_heat_label(self, temp, label):
        assert wo.heat_label(temp) == label


class TestTrend:

    def test_slope_per_hour(self):
        rows = [(at(10), 20.0), (at(11), 21.0), (at(12), 22.0)]
        assert wo.temperature_trend(rows, now=at(12, 5)) == 1.0

    def test_needs_at_least_an_hour_of_data(self):
        assert wo.temperature_trend([(at(11, 30), 20.0), (at(12), 21.0)], now=at(12)) is None


FORECAST = [(at(h), t) for h, t in [(10, 20.0), (11, 21.0), (12, 22.0), (13, 23.0), (14, 23.5), (15, 23.0),
                                    (16, 22.0), (20, 18.0), (23, 16.0)]]


class TestOutlook:

    def test_max_ahead_is_bias_corrected(self):
        # Stasiun 1° lebih panas dari model pada jam 12 → puncak model 23.5 jadi 24.5 pada 14:00
        out = wo.outlook("highest", SEOUL, D, observed=23.0, observed_at=at(12), current=23.0, current_at=at(12),
                         forecast_c=FORECAST, unit="C", now=at(12, 10))
        assert out["passed"] is False and out["value"] == 24.5 and out["at"].hour == 14

    def test_peak_passed_keeps_observed_max(self):
        out = wo.outlook("highest", SEOUL, D, observed=24.0, observed_at=at(14), current=22.0, current_at=at(16),
                         forecast_c=FORECAST, unit="C", now=at(16, 5))
        assert out["passed"] is True and out["value"] == 24.0

    def test_min_can_still_drop_before_midnight(self):
        out = wo.outlook("lowest", SEOUL, D, observed=19.0, observed_at=at(5), current=22.0, current_at=at(16),
                         forecast_c=FORECAST, unit="C", now=at(16, 5))
        assert out["passed"] is False and out["value"] == 16.0 and out["at"].hour == 23

    def test_fahrenheit(self):
        out = wo.outlook("highest", SEOUL, D, observed=72.0, observed_at=at(12), current=71.6, current_at=at(12),
                         forecast_c=FORECAST, unit="F", now=at(12, 10))
        assert out["value"] == 74.3  # 23.5°C = 74.3°F, bias 71.6 − 71.6 = 0

    def test_no_observation(self):
        assert wo.outlook("highest", SEOUL, D, None, None, None, None, FORECAST, "C", at(12)) is None


class TestSummary:

    def test_rising_under_clouds(self):
        text = wo.summarize("highest", "C", 22.0, {"emoji": "☁️", "label": "Mendung", "dim": True}, 0.8,
                            {"value": 24.4, "at": datetime(2026, 9, 28, 14, 0, tzinfo=SEOUL), "passed": False},
                            now_local=datetime(2026, 9, 28, 12, 0, tzinfo=SEOUL))
        assert text == ("☁️ Cuaca sekarang mendung (sejuk); suhu 22°C, naik +0.8°/jam. Kemungkinan suhu max "
                        "±24°C sekitar jam 14:00, perlu +2.4° (+1.2°/jam) — awan tebal/hujan bisa menahan kenaikan.")

    def test_peak_passed(self):
        text = wo.summarize("highest", "C", 31.0, {"emoji": "☀️", "label": "Cerah", "dim": False}, -0.6,
                            {"value": 33.0, "at": datetime(2026, 9, 28, 13, 30, tzinfo=SEOUL), "passed": True},
                            now_local=datetime(2026, 9, 28, 16, 0, tzinfo=SEOUL))
        assert "Cuaca sekarang cerah (panas); suhu 31°C, turun -0.6°/jam" in text
        assert "Puncak kemungkinan sudah lewat — max hari ini kemungkinan tetap 33°C (tercatat 13:30)." in text


def test_peak_passed_hint_overrides_small_forecast_noise():
    out = wo.outlook("highest", SEOUL, D, observed=23.0, observed_at=at(11, 30), current=23.0, current_at=at(15),
                     forecast_c=FORECAST, unit="C", now=at(15, 30), peak_passed_hint=True)
    assert out["passed"] is True and out["value"] == 23.0
