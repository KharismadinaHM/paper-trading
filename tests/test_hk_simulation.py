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
             "model": 0.6, "market_prob": 0.40, "lo": 31, "hi": 31}]
    analysis = {"temp": 28.0, "max": {"observed": 28.4, "mu": 31.0, "sigma": 0.8, "source": "simulasi", "projected": 31.0,
                                      "lead": 3.9, "brackets": rows}}
    d = hk_bot.evaluate("hk_max", NOW, analysis)
    assert "puncak masih ±3.9 jam lagi" in d["skip_reason"]
    analysis["max"]["lead"] = 1.5
    assert hk_bot.evaluate("hk_max", NOW, analysis)["skip_reason"] is None
    rows[0]["market_prob"] = 0.20
    assert "model terlalu berbeda dengan pasar (60% vs 20%" in hk_bot.evaluate("hk_max", NOW, analysis)["skip_reason"]
    rows[0]["market_prob"] = 0.40
    analysis["max"]["sim"] = {"n_paths": 5, "n_sims": 40, "p10": 30.0, "p50": 30.5, "p90": 31.0, "p_observed_holds": 0.5}
    assert "simulasi hanya 5 jalur" in hk_bot.evaluate("hk_max", NOW, analysis)["skip_reason"]
    analysis["max"].pop("sim")
    rows[0]["ask"] = 0.10
    monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": 0.10, "fee": 0.01, "spread": 0.02, "shares": 9, "slippage": 0})
    assert "harga min 15¢" in hk_bot.evaluate("hk_max", NOW, analysis)["skip_reason"]   # tiket lotre ditolak


# --- Sisi NO: hanya bracket yang (hampir) mustahil --------------------------------------

def no_rows():
    return [
        {"bracket": "29°C", "yes_token_id": "t29", "market_id": "m29", "ask": 0.08, "bid": 0.06, "lo": 29, "hi": 29,
         "model": 0.0, "market_prob": 0.07, "prob": 0.028},          # mustahil: max terukur 30.4
        {"bracket": "30°C", "yes_token_id": "t30", "market_id": "m30", "ask": 0.55, "bid": 0.53, "lo": 30, "hi": 30,
         "model": 0.60, "market_prob": 0.54, "prob": 0.58},
        {"bracket": "33°C or higher", "yes_token_id": "t33", "market_id": "m33", "ask": 0.12, "bid": 0.10, "lo": 33,
         "hi": math.inf, "model": 0.02, "market_prob": 0.11, "prob": 0.056},  # hampir mustahil menurut model
    ]


@pytest.fixture
def no_book(monkeypatch):
    from app.paper_trading import autotrader as at
    bids = {"t29": 0.06, "t30": 0.53, "t33": 0.10}
    monkeypatch.setattr(at, "_book_no_side", lambda token, usd: {"price": round(1 - bids[token], 3), "fee": 0.004,
                                                                   "spread": 0.02, "shares": 1, "slippage": 0})
    monkeypatch.setattr(at, "already_decided", lambda key: False)
    return bids


def test_no_buys_impossible_bracket_even_before_peak(no_book):
    analysis = {"max": {"kind": "highest", "observed": 30.4, "mu": 31.2, "sigma": 0.6, "lead": 3.0, "brackets": no_rows()}}
    d = hk_bot.evaluate_no("hk_max_no", NOW, analysis)
    assert d["side"] == "NO" and d["features"]["bracket"] == "29°C" and d["features"]["impossible"]
    assert d["prob"] == 1.0 and d["edge"] == pytest.approx(1 - 0.94 - 0.004) and d["skip_reason"] is None
    assert "mustahil: max terukur 30.4" in d["detail"] and d["title"].endswith("NO 29°C")


def test_no_near_impossible_needs_low_model_prob_and_timing(no_book):
    rows = [r for r in no_rows() if r["bracket"] != "29°C"]
    analysis = {"max": {"kind": "highest", "observed": 30.4, "mu": 31.2, "sigma": 0.6, "lead": 3.0, "brackets": rows}}
    d = hk_bot.evaluate_no("hk_max_no", NOW, analysis)
    assert d["features"]["bracket"] == "33°C or higher" and "puncak masih ±3.0 jam lagi" in d["skip_reason"]
    analysis["max"]["lead"] = 1.0
    d = hk_bot.evaluate_no("hk_max_no", NOW, analysis)
    assert d["skip_reason"] is None and d["edge"] == pytest.approx((1 - 0.056) - 0.90 - 0.004)
    rows[1]["model"] = 0.10                                           # 10% bukan "hampir mustahil"
    assert hk_bot.evaluate_no("hk_max_no", NOW, analysis) is None


def test_no_rejects_expensive_or_thin_edge(no_book):
    rows = no_rows()
    rows[0]["bid"] = 0.01
    analysis = {"max": {"kind": "highest", "observed": 30.4, "mu": 31.2, "sigma": 0.6, "lead": 0.5, "brackets": rows}}
    no_book["t29"] = 0.01                                            # NO 99¢: untung terlalu tipis, pilih 33°C
    assert hk_bot.evaluate_no("hk_max_no", NOW, analysis)["features"]["bracket"] == "33°C or higher"
    analysis["max"]["brackets"] = [r for r in no_rows() if r["bracket"] == "29°C"]
    analysis["max"]["brackets"][0]["bid"] = 0.01
    assert hk_bot.evaluate_no("hk_max_no", NOW, analysis)["skip_reason"] == "harga NO di atas 97.0¢"
    no_book["t29"] = 0.05                                            # NO 95¢ + fee: edge 4.6¢ ≥ 3¢ → beli
    analysis["max"]["brackets"][0]["bid"] = 0.05
    assert hk_bot.evaluate_no("hk_max_no", NOW, analysis)["skip_reason"] is None


def test_min_impossible_and_no_book_mirror(monkeypatch):
    assert hk_bot.impossible_by_observation("lowest", (26, math.inf), 25.4)
    assert not hk_bot.impossible_by_observation("lowest", (25, 25), 25.4)
    assert hk_bot.impossible_by_observation("highest", (-math.inf, 29), 30.0)
    from app.paper_trading import autotrader as at
    monkeypatch.setattr("app.paper_trading.live_market_data.fetch_order_books",
                        lambda tokens, ttl=None: {"t": {"ask": 0.12, "bid": 0.10, "bids": [(0.10, 5.0), (0.09, 100.0)]}})
    monkeypatch.setattr("app.paper_trading.live_market_data.fetch_fee_rate", lambda token: 0.07)
    monkeypatch.setattr(at, "cfg", lambda name: 0.0 if name == "SLIPPAGE" else at.EDITABLE_CONFIG and 0)
    b = at._book_no_side("t", 10.0)   # NO 90¢ × 5 sh = $4.5, sisanya di 91¢
    assert b["book_price"] == pytest.approx(10.0 / (5 + 5.5 / 0.91), abs=1e-3) and b["spread"] == pytest.approx(0.02)


def test_station_bias_profile_and_correction(monkeypatch):
    """HKO kota lebih hangat dari model (terutama malam): koreksi jauh ke depan memudar ke profil, bukan ke nol."""
    from decimal import Decimal
    from app.core.database import get_db_session
    from app.paper_trading import hko_hourly as hh
    from app.paper_trading.models import StationReading
    monkeypatch.setattr(hh, "_profile_cache", {"at": 0.0, "value": None})
    now = datetime(2026, 10, 14, 12, 0, tzinfo=HKT).astimezone(timezone.utc)
    model, db = [], get_db_session()
    for day in range(1, 8):
        start = datetime(2026, 10, 14, 0, tzinfo=HKT) - timedelta(days=day)
        for i in range(144):
            at = start + timedelta(minutes=10 * i)
            base = 26 + 4 * math.sin(math.pi * max(0, at.hour - 6) / 14)
            night = at.hour < 6 or at.hour >= 20
            temp = base + (2.0 if night else 0.5) + (0.3 if (at.hour == 14 and at.minute == 20) else 0.0)
            db.add(StationReading(station="HKO", observed_at=at.astimezone(timezone.utc), temp=Decimal(str(round(temp, 2)))))
            if at.minute == 0:
                model.append((at.astimezone(timezone.utc), base))
    db.commit()
    db.close()
    monkeypatch.setattr(hh, "fetch_model_series", lambda start, end: model)
    p = hh.station_bias_profile(now, refresh=True)
    assert p["days"] == 7 and p["hourly"][2] == pytest.approx(2.0 * 7 / 12, abs=0.05)   # malam: +2°C (ditarik ke 0)
    assert p["hourly"][13] == pytest.approx(0.5 * 7 / 12, abs=0.05) and p["peak_excess"] == pytest.approx(0.2, abs=0.05)  # 14:20 30.7 vs 13:00 30.5
    far = datetime(2026, 10, 15, 2, tzinfo=HKT)
    assert hh.correction(far, 14, 0.0, p) == pytest.approx(0.5 * p["hourly"][2])         # offset 0, lead jauh: separuh profil
    assert hh.correction(far, 0.5, 1.2, p) == pytest.approx(1.2)                           # dekat: ikut bacaan terkini


def test_hk_signals_api():
    from fastapi.testclient import TestClient
    from app.dashboard import app
    data = TestClient(app).get("/api/hk/signals").json()
    assert set(data) == {"hk_max", "hk_min", "hk_max_no", "hk_min_no"} and data["hk_min"]["signals"] == []
    html = TestClient(app).get("/hk").text
    assert 'id="histKind"' in html and 'id="sigBox"' in html
