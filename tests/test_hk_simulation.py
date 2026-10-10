"""Test simulasi Monte Carlo HK: gabungan jalur model, noise historis, hujan, resmi HKO, klimatologi; aturan masuk."""
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.paper_trading import hk_bot, hk_simulation as sim

HKT = ZoneInfo("Asia/Hong_Kong")
NOW = datetime(2026, 10, 10, 11, 0, tzinfo=HKT).astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def fixed_sigma(monkeypatch):
    monkeypatch.setattr(hk_bot, "sigma_for", lambda lead, now=None: 0.5)


def path(name, peak, trough=25.0, rain=False):
    """Jalur per jam HKT 10 Okt: naik ke `peak` jam 14, turun ke `trough` jam 23."""
    series, precip = [], {}
    for h in range(0, 48):
        ts = datetime(2026, 10, 10, 0, tzinfo=HKT) + timedelta(hours=h)
        hh = h % 24
        if hh <= 14:
            v = trough + (peak - trough) * max(0, hh - 6) / 8
        else:
            v = peak - (peak - trough) * (hh - 14) / 9
        series.append((ts.astimezone(timezone.utc), round(v, 2)))
        if rain and 11 <= h <= 13:
            precip[ts.astimezone(timezone.utc)] = 2.0
    return {"name": name, "series": series, "precip": precip}


PATHS = [path("a", 30.0), path("b", 31.0), path("c", 32.0)]


def run(**kw):
    args = dict(kind="highest", now=NOW, observed=29.0, latest_at=None, latest_temp=None, paths=PATHS, clim=[])
    args.update(kw)
    return sim.simulate(**args)


def test_simulation_combines_paths_and_noise():
    r = run()
    assert r["n_paths"] == 3 and r["n_sims"] == 24 and len(r["samples"]) == 24
    assert r["mean"] == pytest.approx(31.0, abs=0.4) and 0.5 < r["std"] < 1.5
    assert all(x >= 29.0 for x in r["samples"])                      # max tidak bisa di bawah terukur
    assert r["lead_extreme"] == pytest.approx(3.0, abs=1.0)           # puncak ±14:00, sekarang 11:00
    probs = sim.bracket_probs(r["samples"], [(-math.inf, 29), (30, 30), (31, 31), (32, math.inf)])
    assert sum(probs) == pytest.approx(1.0) and probs[0] < 0.4 and probs[3] > 0.1


def test_official_hint_rain_shock_bias_and_history():
    base = run()
    assert run(hint=33.0)["mean"] > base["mean"] + 0.5                # digeser ke angka resmi HKO
    assert run(rain_expected=True)["mean"] < base["mean"] - 0.3        # kejutan hujan
    wet = run(rain_expected=True, paths=[path("w", 31.0, rain=True)] * 3)
    assert wet["mean"] == pytest.approx(31.0, abs=0.5)                 # jalur yang sudah hujan tidak didobel
    assert run(bias=0.5)["mean"] == pytest.approx(base["mean"] + 0.5, abs=0.05)
    hist = run(clim=[26.0] * 50)                                       # data historis: porsi kecil, menyusut dekat puncak
    assert 0 < hist["clim_weight"] <= sim.CLIM_WEIGHT_MAX and hist["n_clim"] >= 1
    tomorrow = run(observed=None, day_offset=1, clim=[26.0] * 50)
    assert tomorrow["clim_weight"] == sim.CLIM_WEIGHT_MAX and tomorrow["mean"] < 31.0


def test_late_day_observed_max_holds():
    late = datetime(2026, 10, 10, 17, 0, tzinfo=HKT).astimezone(timezone.utc)
    r = run(now=late, observed=31.5)
    assert r["p_observed_holds"] == 1.0 and r["lead_extreme"] == 0.0 and r["clim_weight"] == 0.0


def test_impossible_brackets_stay_zero():
    est = {"observed": 30.4, "mu": 30.6, "sigma": 0.5}
    probs = hk_bot._model_probs([(29, 29), (30, 30), (31, 31)], "highest", est, [29.5, 30.5, 30.6, 31.2])
    assert probs[0] == 0.0 and sum(probs) == pytest.approx(1.0)


def test_entry_waits_until_close_to_peak(monkeypatch):
    from app.paper_trading import autotrader as at
    monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": 0.30, "fee": 0.01, "spread": 0.02, "shares": 3, "slippage": 0})
    monkeypatch.setattr(at, "already_decided", lambda key: False)
    rows = [{"bracket": "31°C", "yes_token_id": "t", "market_id": "m", "ask": 0.30, "bid": 0.28, "prob": 0.6,
             "model": 0.6, "market_prob": 0.29, "lo": 31, "hi": 31}]
    analysis = {"temp": 28.0, "max": {"observed": 28.4, "mu": 31.0, "sigma": 0.8, "source": "simulasi", "projected": 31.0,
                                      "lead": 3.9, "brackets": rows}}
    d = hk_bot.evaluate("hk_max", NOW, analysis)
    assert "puncak masih ±3.9 jam lagi" in d["skip_reason"]
    analysis["max"]["lead"] = 1.5
    assert hk_bot.evaluate("hk_max", NOW, analysis)["skip_reason"] is None
    rows[0]["ask"] = 0.10
    monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": 0.10, "fee": 0.01, "spread": 0.02, "shares": 9, "slippage": 0})
    assert "harga min 15¢" in hk_bot.evaluate("hk_max", NOW, analysis)["skip_reason"]   # tiket lotre ditolak
