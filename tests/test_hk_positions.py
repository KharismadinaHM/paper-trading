"""Test manajemen posisi HK: nilai jual dari order book, keputusan exit, mode bayangan vs live, beli ulang, penilaian."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.core.database import get_db_session
from app.paper_trading import autotrader as at, hk_positions as hp
from app.paper_trading.models import AutotradeSignal

HKT = ZoneInfo("Asia/Hong_Kong")
NOW = datetime(2026, 10, 14, 13, 0, tzinfo=HKT).astimezone(timezone.utc)
BOOK = {"tok30": {"ask": 0.45, "bid": 0.40, "asks": [(0.45, 50.0)], "bids": [(0.40, 3.0), (0.38, 100.0)]}}


@pytest.fixture(autouse=True)
def book(monkeypatch):
    monkeypatch.setattr("app.paper_trading.live_market_data.fetch_order_books", lambda tokens, ttl=None: BOOK)
    monkeypatch.setattr("app.paper_trading.live_market_data.fetch_fee_rate", lambda token: 0.07)


def analysis(prob=0.25, observed=30.4):
    row = {"bracket": "30°C", "market_id": "m30", "yes_token_id": "tok30", "lo": 30, "hi": 30, "prob": prob,
           "model": prob, "market_prob": 0.42, "ask": 0.45, "bid": 0.40}
    return {"max": {"kind": "highest", "observed": observed, "mu": 30.9, "sigma": 0.4, "brackets": [row]}}


def pos(side="YES", strategy="hk_max", minutes=60):
    return {"market_id": "m30", "side": side, "shares": 5.0, "entry": 0.20, "size": 1.0, "strategy": strategy,
            "opened_at": NOW - timedelta(minutes=minutes), "market_name": "HK 30°C"}


def test_sell_value_walks_bids_and_mirrors_no():
    yes = hp.sell_value("tok30", "YES", 5.0)                 # 3 sh @40¢ + 2 sh @38¢
    assert yes["bid"] == pytest.approx((3 * 0.40 + 2 * 0.38) / 5)
    assert yes["net"] == pytest.approx(yes["bid"] - 0.07 * yes["bid"] * (1 - yes["bid"]) - float(at.cfg("SLIPPAGE")), abs=1e-4)
    no = hp.sell_value("tok30", "NO", 5.0)                    # NO dijual ke 1 − ask YES = 55¢
    assert no["bid"] == pytest.approx(0.55)
    assert hp.sell_value("tok30", "YES", 500.0) is None       # kedalaman tidak cukup


def test_exit_when_market_values_position_above_model():
    ev = hp.evaluate_exit(pos(), NOW, analysis(prob=0.25))
    assert ev["should_exit"] and ev["edge"] == pytest.approx(ev["sell"]["net"] - 0.25)
    hold = hp.evaluate_exit(pos(), NOW, analysis(prob=0.45))
    assert not hold["should_exit"] and "tahan" in hold["reason"]
    young = hp.evaluate_exit(pos(minutes=5), NOW, analysis(prob=0.25))
    assert not young["should_exit"] and "jeda" in young["reason"]
    # NO pada bracket yang sudah mustahil (max terukur 31.2 ≥ 31) tidak pernah dijual
    no = hp.evaluate_exit(pos(side="NO", strategy="hk_max_no"), NOW, analysis(prob=0.0, observed=31.2))
    assert no["p_hold"] == 1.0 and not no["should_exit"]


def test_shadow_mode_records_once_and_does_not_sell(monkeypatch):
    sent = []
    monkeypatch.setattr(hp, "open_positions", lambda now: [pos()])
    with patch("app.paper_trading.telegram.send_telegram_message", side_effect=lambda t, **k: sent.append(t) or {"success": True}), \
         patch("app.paper_service.sell_paper_position", side_effect=AssertionError("tidak boleh menjual")):
        assert [a["mode"] for a in hp.manage(NOW, analysis(), ["hk_max"])] == ["shadow"]
        assert hp.manage(NOW + timedelta(minutes=5), analysis(), ["hk_max"]) == []   # sekali per posisi per hari
    assert any("HK JUAL (bayangan)" in t for t in sent)
    db = get_db_session()
    sig = db.query(AutotradeSignal).filter_by(strategy="hk_exit").one()
    assert sig.action == "shadow" and sig.side == "YES"
    db.close()


def test_live_mode_sells_and_allows_reentry_in_another_bracket(monkeypatch):
    at.set_config({"HK_SWITCH_LIVE": True})
    monkeypatch.setattr(hp, "open_positions", lambda now: [pos()])
    with patch("app.paper_trading.telegram.send_telegram_message", return_value={"success": True}), \
         patch("app.paper_service.sell_paper_position", return_value={"realized_pnl": 0.9}) as sell:
        acts = hp.manage(NOW, analysis(), ["hk_max"])
    assert acts[0]["mode"] == "live" and sell.call_args[0][:2] == ("m30", "YES")
    key, sold = hp.entry_key("hk_max", NOW)
    assert key.endswith("|r1") and sold == {"m30"}                 # boleh beli lagi, bukan bracket yang baru dijual
    n, _, last = hp.exits_today("hk_max", NOW)
    assert n == 1 and last is not None
    at.set_config({"HK_MAX_SWITCHES": 1})
    ev = hp.evaluate_exit(pos(minutes=120), NOW + timedelta(hours=1), analysis())
    assert not ev["should_exit"] and "maks 1" in ev["reason"]


def test_switch_report_scores_resolved_exits(monkeypatch):
    monkeypatch.setattr(hp, "open_positions", lambda now: [pos()])
    with patch("app.paper_trading.telegram.send_telegram_message", return_value={"success": True}):
        hp.manage(NOW, analysis(), ["hk_max"])
    db = get_db_session()
    sig = db.query(AutotradeSignal).filter_by(strategy="hk_exit").one()
    sig.outcome, sig.pnl_per_share = "LOSS", Decimal(str(-float(sig.price)))   # YES kalah: jual menyelamatkan nilai jualnya
    db.commit()
    db.close()
    rep = hp.switch_report(now=NOW + timedelta(days=1))
    shadow = rep["modes"]["shadow"]
    assert shadow == {**shadow, "n": 1, "resolved": 1, "saved": 1, "mistakes": 0} and shadow["net_usd"] > 0
    from fastapi.testclient import TestClient
    from app.dashboard import app
    data = TestClient(app).get("/api/hk/positions").json()
    assert data["live"] is False and "modes" in data["report"]
    assert 'id="posRows"' in TestClient(app).get("/hk").text
