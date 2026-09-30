"""
Test riset auto trader: pencatatan sinyal (ditrade & dilewati), pelacak hasil, laporan, saran
ambang (satu entri per market), command /autoresearch, API & CSV.
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.core.database import get_db_session
from app.paper_trading import autotrade_research as ar
from app.paper_trading import autotrader as at
from app.paper_trading.models import AutotradeSignal, MarketResolution
from app.paper_trading.telegram_bot import handle_incoming_message

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def add_signal(key, strategy="btc15", market="0xm", side="YES", prob=0.6, price=0.5, fee=0.0175, action="skipped",
               reason="edge di bawah minimum", created=None, outcome=None, features=None):
    edge = prob - price - fee
    db = get_db_session()
    try:
        db.add(AutotradeSignal(signal_key=key, strategy=strategy, market_id=market, label=key, side=side,
                               model_prob=Decimal(str(prob)), price=Decimal(str(price)), fee=Decimal(str(fee)),
                               edge=Decimal(str(round(edge, 4))), action=action, skip_reason=reason,
                               features=json.dumps(features or {}), local_day="d", created_at=created or NOW,
                               outcome=outcome,
                               pnl_per_share=None if outcome is None else Decimal(str(
                                   (1 - price - fee) if outcome == "WIN" else -(price + fee)))))
        db.commit()
    finally:
        db.close()


def resolve(market, winner):
    db = get_db_session()
    try:
        db.add(MarketResolution(market_id=market, winning_outcome=winner))
        db.commit()
    finally:
        db.close()


class TestLogging:

    def test_btc_tick_logs_skipped_signal_with_reason_and_features_once_per_bucket(self, monkeypatch):
        start = NOW.replace(minute=0, second=0)
        now = start + timedelta(minutes=41)
        monkeypatch.setattr(at, "_btc_market", lambda s, series="btc": {"condition_id": "0xb", "title": "BTC",
                                                                         "up": "tu", "down": "td", "accepting": True, "slug": "s"})
        monkeypatch.setattr(at, "_btc_klines", lambda: [])
        monkeypatch.setattr(at, "btc_model", lambda k, s, n, d=60: {"p_up": 0.52, "price": 100.0, "open": 99.9,
                                                                    "change_pct": 0.1, "minutes_left": 19, "sigma": 0.0004})
        monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": 0.51, "fee": 0.0175, "spread": 0.01, "shares": 9})
        assert at.btc_tick(now) is None
        at.btc_tick(now + timedelta(seconds=30))  # ember 5 menit yang sama → tidak dicatat ulang
        rows = ar.load_signals()
        assert len(rows) == 1
        r = rows[0]
        assert r["action"] == "skipped" and r["skip_reason"] == "edge di bawah minimum"
        assert r["features"]["minute"] == 41.0 and r["features"]["sigma_1m"] == 0.0004 and r["features"]["spread"] == 0.01

    def test_weather_disagreement_logged(self, monkeypatch):
        from tests.test_autotrader import TestStrategies
        monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": 0.25, "fee": 0.0131, "spread": 0.02, "shares": 20})
        ev = TestStrategies()._event(favorite_first=False)
        result = at.weather_evaluate(ev, NOW)
        assert result["skip_reason"] == "beda dengan favorit pasar" and result["features"]["agree"] is False
        assert result["features"]["city"] == "Tokyo" and at.weather_decision(ev, NOW) is None


class TestTracking:

    def test_outcomes_filled_after_wait(self):
        add_signal("a", market="0xa", side="YES", created=NOW - timedelta(minutes=30))
        add_signal("b", market="0xb", side="NO", created=NOW - timedelta(minutes=30))
        add_signal("fresh", market="0xc", created=NOW - timedelta(minutes=5))  # belum waktunya dicek
        resolve("0xa", "YES")
        resolve("0xb", "YES")
        with patch("app.market_collector.collector.sync_markets_by_condition_ids") as sync:
            summary = ar.track_signal_outcomes(now=NOW)
        sync.assert_not_called()  # resolusi sudah ada di database
        assert summary["resolved"] == 2
        by_key = {r["label"]: r for r in ar.load_signals()}
        assert by_key["a"]["outcome"] == "WIN" and by_key["a"]["pnl"] == pytest.approx(0.4825)
        assert by_key["b"]["outcome"] == "LOSS" and by_key["b"]["pnl"] == pytest.approx(-0.5175)
        assert by_key["fresh"]["outcome"] is None

    def test_missing_resolution_is_synced_and_throttled(self):
        add_signal("x", market="0xx", created=NOW - timedelta(hours=2))
        with patch("app.market_collector.collector.sync_markets_by_condition_ids") as sync:
            ar.track_signal_outcomes(now=NOW)
            ar.track_signal_outcomes(now=NOW + timedelta(minutes=5))  # dalam jeda cek ulang
        assert sync.call_count == 1


class TestReport:

    def _many(self, n_markets, edge_prob, win_every, strategy="btc15", reason="edge di bawah minimum"):
        for i in range(n_markets):
            outcome = "WIN" if i % win_every == 0 else "LOSS"
            add_signal(f"{strategy}-{edge_prob}-{i}", strategy=strategy, market=f"0x{strategy}{edge_prob}{i}",
                       prob=edge_prob, price=0.5, reason=reason, outcome=outcome,
                       created=NOW - timedelta(minutes=i), features={"minute": 8 + i % 5})

    def test_first_sample_per_market(self):
        rows = [{"market_id": "m", "edge": 0.01, "created_at": NOW}, {"market_id": "m", "edge": 0.06,
                                                                     "created_at": NOW + timedelta(minutes=1)},
                {"market_id": "m", "edge": 0.09, "created_at": NOW + timedelta(minutes=2)}]
        assert [r["edge"] for r in ar.first_per_market(rows, 0.05)] == [0.06]

    def test_suggests_better_threshold(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTOTRADE_BTC_MIN_EDGE", 0.05)
        self._many(40, edge_prob=0.53, win_every=3)   # edge ≈ 1¢, menang 1/3 → rugi
        self._many(40, edge_prob=0.57, win_every=3)   # edge ≈ 5¢, menang 1/3 → rugi (lolos ambang 5¢)
        self._many(40, edge_prob=0.60, win_every=1)   # edge ≈ 8¢, selalu menang
        tips = " ".join(ar.research_report()["suggestions"])
        assert "btc15: ambang edge 8¢" in tips and "BTC_MIN_EDGE = 0.08" in tips

    def test_insufficient_data_message(self):
        self._many(5, edge_prob=0.6, win_every=2)
        assert "data belum cukup" in " ".join(ar.research_report()["suggestions"])

    def test_losing_strategy_flagged(self):
        self._many(40, edge_prob=0.53, win_every=4)
        assert "pertimbangkan menonaktifkan" in " ".join(ar.research_report()["suggestions"])

    def test_report_sections(self):
        self._many(12, edge_prob=0.6, win_every=2)
        st = ar.research_report()["strategies"]["btc15"]
        assert st["samples"] == 12 and st["resolved"] == 12 and st["all"]["win_rate"] == 0.5
        assert any(b["n"] for b in st["edge_buckets"]) and st["by_minute"] and st["calibration"]

    def test_telegram_and_markdown_safety(self):
        self._many(3, edge_prob=0.6, win_every=2, strategy="weather_post", reason="beda dengan favorit pasar")
        text = handle_incoming_message("/autoresearch", sender_chat_id="1", allowed_chat_id="1")
        assert "Riset auto trader" in text and "weather\\_post" in text and "weather_post" not in text.replace("\\_", "")
        status = handle_incoming_message("/autostats", sender_chat_id="1", allowed_chat_id="1")
        assert "maker\\_btc" in status
        assert "/autoresearch" in handle_incoming_message("/help", sender_chat_id="1", allowed_chat_id="1")

    def test_api_and_csv(self):
        from fastapi.testclient import TestClient
        from app.dashboard import app
        self._many(3, edge_prob=0.6, win_every=2)
        client = TestClient(app)
        assert "btc15" in client.get("/api/autotrade/research").json()["strategies"]
        csv_text = client.get("/api/autotrade/signals.csv").text
        header = csv_text.splitlines()[0]
        assert header.startswith("created_at,strategy,market_id") and "minute" in header
        assert len(csv_text.strip().splitlines()) == 4


def test_tie_keeps_current_threshold(monkeypatch):
    monkeypatch.setattr(settings, "AUTOTRADE_BTC_MIN_EDGE", 0.05)
    TestReport()._many(40, edge_prob=0.60, win_every=1)  # semua ambang 0–8¢ memberi hasil sama
    assert "ambang sekarang (5¢) sudah terbaik" in " ".join(ar.research_report()["suggestions"])
