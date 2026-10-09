"""Test kalibrasi otomatis bot HK: bias per jam, bobot model vs pasar, pengaman, jadwal, perintah & API."""
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.core.database import get_db_session
from app.paper_trading import hk_calibration as hc
from app.paper_trading.models import AutotradeSignal
from app.paper_trading.telegram_bot import handle_incoming_message

HKT = ZoneInfo("Asia/Hong_Kong")
TODAY = date(2026, 10, 20)
NOW = datetime(2026, 10, 20, 1, 0, tzinfo=HKT).astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(hc, "_cache", {"at": 0.0, "value": None})
    monkeypatch.setattr("app.paper_trading.hk_ai.actual_extremes", lambda day: {"max": 30.6, "min": 25.0})


def add_signals(days, kind="hk_max", hour=10, mu_raw=30.0, observed=29.0, model=None, market=None, outcomes=None, today=TODAY):
    db = get_db_session()
    for i in range(days):
        day = today - timedelta(days=i + 1)
        at = datetime.combine(day, datetime.min.time(), tzinfo=HKT) + timedelta(hours=hour)
        features = {"mu_raw": mu_raw, "observed": observed, "hour_hkt": hour, "model_raw": model, "market_prob": market}
        db.add(AutotradeSignal(signal_key=f"{kind}|{day}|{hour}", strategy=kind, market_id=f"0x{kind}{i}", side="YES",
                               model_prob=Decimal("0.5"), price=Decimal("0.5"), fee=Decimal("0.01"), edge=Decimal("0"),
                               action="skipped", features=json.dumps(features), local_day=day.isoformat(),
                               created_at=at.astimezone(timezone.utc),
                               outcome=(outcomes[i % len(outcomes)] if outcomes else None)))
    db.commit()
    db.close()


def test_bias_needs_enough_days_shrinks_and_steps():
    add_signals(5)  # model mentah 30.0 vs hasil 30.6 → meremehkan 0.6°C, tapi baru 5 hari
    cal = hc.calibrate(NOW)
    assert cal["bias"]["max"]["09–12"] == {"value": 0.0, "target": 0.0, "mean": 0.6, "days": 5}
    add_signals(12, hour=11)
    cal = hc.calibrate(NOW)
    e = cal["bias"]["max"]["09–12"]
    assert e["days"] == 12 and e["target"] == pytest.approx(0.6 * 12 / 17, abs=0.01)
    assert e["value"] == pytest.approx(0.3)                       # langkah maks 0.3°C per hari
    assert hc.calibrate(NOW)["bias"]["max"]["09–12"]["value"] == pytest.approx(0.42, abs=0.01)
    assert cal["bias"]["max"]["15–18"]["days"] == 0 and cal["bias"]["min"]["09–12"]["value"] == 0.0


def test_bias_uses_observed_when_peak_already_passed():
    add_signals(12, hour=16, mu_raw=29.0, observed=30.6)  # puncak sudah lewat: perkiraan = terukur 30.6 = hasil
    assert hc.calibrate(NOW)["bias"]["max"]["15–18"]["mean"] == 0.0


def test_bias_applied_to_estimate_only_when_enabled(monkeypatch):
    from app.paper_trading import hk_bot
    add_signals(12)
    hc.calibrate(NOW)
    monkeypatch.setattr(hk_bot, "sigma_for", lambda lead, now=None: 0.5)
    now = datetime(2026, 10, 20, 10, 30, tzinfo=HKT).astimezone(timezone.utc)
    status = {"temp": 29.5, "max": 29.6, "min": 25.0, "official": {}}
    est = hk_bot.estimate("highest", now, status, [(datetime(2026, 10, 20, 14, tzinfo=HKT), 30.2)])
    assert est["mu_raw"] == 30.2 and est["bias"] == pytest.approx(0.3) and est["mu"] == pytest.approx(30.5)
    from app.paper_trading import autotrader as at
    at.set_config({"HK_AUTO_CALIBRATE": False})
    hc._cache.update(at=0.0, value=None)
    assert hk_bot.estimate("highest", now, status, [(datetime(2026, 10, 20, 14, tzinfo=HKT), 30.2)])["bias"] == 0.0


def test_weight_learns_toward_market_with_guards():
    # model yakin 90%, pasar 50%, hasilnya separuh menang: pasar lebih akurat → bobot model turun
    add_signals(12, model=0.9, market=0.5, outcomes=["WIN", "LOSS"])
    w = hc.calibrate(NOW)["weight"]
    assert w["best"] == 0.0 and w["days"] == 12 and w["logloss_market"] < w["logloss_model"]
    assert w["target"] == pytest.approx(0.6 - 0.6 * 12 / 32, abs=0.01)
    assert w["value"] == pytest.approx(0.5)                       # langkah maks 0.1 per hari
    assert hc.model_weight() == pytest.approx(0.5)
    from app.paper_trading import autotrader as at
    at.set_config({"HK_MODEL_WEIGHT": 0.8})                      # manual di dashboard selalu menang
    assert hc.model_weight() == pytest.approx(0.8)
    hc.reset(NOW)
    at.set_config({"HK_MODEL_WEIGHT": None})
    assert hc.current() == {} and hc.model_weight() == pytest.approx(settings.AUTOTRADE_HK_MODEL_WEIGHT)


def test_scheduled_once_per_day_after_half_past_midnight(monkeypatch):
    sent = []
    monkeypatch.setattr("app.paper_trading.autotrader.notify", lambda text: sent.append(text) or {"success": True})
    add_signals(3)
    early = datetime(2026, 10, 20, 0, 10, tzinfo=HKT).astimezone(timezone.utc)
    assert hc.maybe_calibrate(early) is False
    assert hc.maybe_calibrate(NOW) is True and hc.maybe_calibrate(NOW + timedelta(hours=2)) is False
    assert len(sent) == 1 and "Kalibrasi otomatis HK" in sent[0] and "*" not in sent[0]


def test_command_and_api():
    assert "Belum ada kalibrasi" in handle_incoming_message("/kalibrasi", "1")
    add_signals(12, today=datetime.now(HKT).date())  # perintah & API memakai jam sekarang
    assert "Bias max: 09–12: +0.30°" in handle_incoming_message("/kalibrasi jalankan", "1")
    assert "/kalibrasi" in handle_incoming_message("/help", "1")
    from fastapi.testclient import TestClient
    from app.dashboard import app
    client = TestClient(app)
    data = client.get("/api/hk/calibration").json()
    assert data["enabled"] and data["calibration"]["bias"]["max"]["09–12"]["days"] == 12 and data["min_days"] == 10
    assert client.post("/api/hk/calibration/run").json()["calibration"]["signals"] == 12
    assert client.delete("/api/hk/calibration").json()["calibration"] == {}
    assert "Kalibrasi HK di-reset" in handle_incoming_message("/kalibrasi reset", "1")
    assert 'id="calibrationBox"' in client.get("/hk").text
