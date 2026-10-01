"""Test tabel per jam HKO: expected (prediksi tersimpan / rekonstruksi model), Δ, real, 6 jam ke depan."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.core.database import get_db_session
from app.paper_trading import hko_hourly as hh
from app.paper_trading.models import StationForecast, StationReading
from app.paper_trading.telegram_bot import handle_incoming_message

HKT = ZoneInfo("Asia/Hong_Kong")


def hkt(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=HKT)


@pytest.fixture
def model(monkeypatch):
    # Model: 28°C jam 00 naik 0.5°/jam, hari 29–30 Sep HKT
    series = [((hkt(29, 0) + timedelta(hours=i)).astimezone(timezone.utc), 28.0 + 0.5 * (i % 24))
              for i in range(-6, 54)]
    monkeypatch.setattr(hh, "fetch_model_series", lambda start, end: series)
    db = get_db_session()
    db.query(StationReading).delete()
    db.query(StationForecast).delete()
    db.commit()
    yield db
    db.close()


def add_reading(db, at, temp):
    db.add(StationReading(station="HKO", observed_at=at.astimezone(timezone.utc), temp=Decimal(str(temp))))
    db.commit()


def test_today_table_has_history_and_six_hours_ahead(model):
    db = model
    for h in range(0, 11):
        add_reading(db, hkt(30, h), 29.0 + 0.5 * h)  # real = model + 1.0
    add_reading(db, hkt(30, 10, 20), 34.3)
    now = hkt(30, 10, 25).astimezone(timezone.utc)
    data = hh.hourly_table(now=now, db=db)
    rows = data["rows"]
    past = [r for r in rows if not r["future"]]
    future = [r for r in rows if r["future"]]
    assert [r["at"].hour for r in past] == list(range(0, 11))
    assert [r["at"].hour for r in future] == [11, 12, 13, 14, 15, 16]
    assert past[5]["real"] == 31.5 and past[5]["delta"] == 0.5
    # rekonstruksi: model + bias 3 jam sebelumnya (+1.0)
    assert past[5]["expected"] == 31.5 and past[5]["source"] == "model" and past[5]["error"] == 0.0
    # proyeksi: model 11:00 = 33.5, bias bacaan 10:20 = 34.3 − (33.0 + 0.5/3) ≈ +1.13
    assert future[0]["expected"] == pytest.approx(34.6, abs=0.05)
    assert future[0]["real"] is None and data["ahead_min"] == future[0]["expected"]


def test_stored_prediction_preferred_when_made_an_hour_before(model):
    db = model
    for h in range(0, 13):
        add_reading(db, hkt(30, h), 29.0 + 0.5 * h)
    now = hkt(30, 9, 3).astimezone(timezone.utc)
    assert hh.record_hourly_forecasts(now=now, db=db) == 6
    assert hh.record_hourly_forecasts(now=now + timedelta(minutes=30), db=db) == 0  # sekali per jam
    db.add(StationForecast(station="HKO", target_at=hkt(30, 12).astimezone(timezone.utc),
                           made_at=hkt(30, 11, 30).astimezone(timezone.utc), value=Decimal("40")))  # <1 jam: diabaikan
    db.commit()
    data = hh.hourly_table(now=hkt(30, 12, 5).astimezone(timezone.utc), db=db)
    row = next(r for r in data["rows"] if r["at"].hour == 12 and not r["future"])
    assert row["source"] == "prediksi" and row["expected"] == 35.0 and row["real"] == 35.0
    assert row["error"] == 0.0


def test_past_day_full_24h_without_projection(model):
    db = model
    for h in range(24):
        add_reading(db, hkt(29, h), 30.0)
    data = hh.hourly_table(day=hkt(29, 0).date(), now=hkt(30, 8).astimezone(timezone.utc), db=db)
    assert len(data["rows"]) == 24 and not any(r["future"] for r in data["rows"])
    assert data["max"] == 30.0 and data["min"] == 30.0


def test_hk_jam_command(model, monkeypatch):
    monkeypatch.setattr("app.paper_trading.hko_hourly.format_hourly", lambda day=None, now=None: f"TABEL {day}")
    assert handle_incoming_message("/hk jam", sender_chat_id="1", allowed_chat_id="1") == "TABEL None"
    assert handle_incoming_message("/hk jam 2026-09-29", sender_chat_id="1", allowed_chat_id="1") == "TABEL 2026-09-29"
    assert "Format tanggal" in handle_incoming_message("/hk jam kemarin", sender_chat_id="1", allowed_chat_id="1")
