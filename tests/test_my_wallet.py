"""
Test portfolio Polymarket sendiri (read-only): ringkasan dari data publik, tanda tangan L2 CLOB,
saldo & open order (opsional), catatan otomatis, command /porto, dan API dashboard.
"""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.paper_trading import live_market_data as live
from app.paper_trading import my_wallet as mw
from app.paper_trading import wallets as wl
from app.paper_trading.telegram_bot import handle_incoming_message

NOW = datetime.now(timezone.utc).replace(microsecond=0)
ME = "0x" + "d" * 40
SECRET = base64.urlsafe_b64encode(b"super-secret-key-bytes").decode()


def position(title, cur, value, cost, pnl, pct, redeemable=False, hours_left=72, outcome="Yes"):
    return {"title": title, "outcome": outcome, "size": value / max(cur, 0.01), "avgPrice": cost / max(value / max(cur, 0.01), 1),
            "curPrice": cur, "currentValue": value, "initialValue": cost, "cashPnl": pnl, "percentPnl": pct,
            "endDate": (NOW + timedelta(hours=hours_left)).isoformat(), "redeemable": redeemable, "eventSlug": "ev"}


POSITIONS = [
    position("Hong Kong 33°C", 0.97, 60.0, 40.0, 20.0, 50.0, hours_left=10),
    position("Tokyo 25°C", 0.30, 15.0, 40.0, -25.0, -62.5),
    position("Seoul 23°C", 1.0, 30.0, 20.0, 10.0, 50.0, redeemable=True),
    position("Paris 18°C", 0.0, 0.0, 12.0, -12.0, -100.0, redeemable=True),
]


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "POLYMARKET_WALLET_ADDRESS", ME)
    for key in ("POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE"):
        monkeypatch.setattr(settings, key, None)

    def fake_get(path, **params):
        if path == "/positions":
            return POSITIONS
        if path == "/value":
            return [{"value": 105.0}]
        if path == "/activity":
            return [{"timestamp": int(NOW.timestamp()) - 60, "type": "TRADE", "side": "BUY", "outcome": "Yes",
                     "title": "Hong Kong 33°C", "price": 0.9, "size": 10, "usdcSize": 9.0}]
        if path == "/closed-positions":
            return [{"realizedPnl": 5, "timestamp": int(NOW.timestamp()) - 3600, "avgPrice": 0.6, "totalBought": 10}]
        if path == "/v1/leaderboard":
            return [{"pnl": {"DAY": 3.5, "WEEK": -8, "MONTH": 42, "ALL": 120}[params["timePeriod"]], "vol": 500}]
        raise AssertionError(path)

    monkeypatch.setattr(wl, "_get", fake_get)
    live.clear_cache()
    yield
    live.clear_cache()


@pytest.fixture
def with_keys(monkeypatch, configured):
    monkeypatch.setattr(settings, "POLYMARKET_API_KEY", "key-1")
    monkeypatch.setattr(settings, "POLYMARKET_API_SECRET", SECRET)
    monkeypatch.setattr(settings, "POLYMARKET_API_PASSPHRASE", "pass-1")


class TestSummary:

    def test_public_data_summary(self, configured):
        s = mw.build_summary(now=NOW)
        assert s["positions_value"] == 105.0 and s["cash"] is None and s["open_orders"] is None
        assert [p["title"] for p in s["positions"]] == ["Hong Kong 33°C", "Tokyo 25°C"]  # urut nilai
        assert s["unrealized_pnl"] == -5.0
        assert s["claimable"]["count"] == 1 and s["claimable"]["value"] == 30.0 and s["lost"]["count"] == 1
        assert {k: v["pnl"] for k, v in s["pnl"].items()} == {"DAY": 3.5, "WEEK": -8, "MONTH": 42, "ALL": 120}
        assert s["stats"]["win_rate"] is not None and s["activity"][0]["side"] == "BUY"

    def test_insights(self, configured):
        tips = " | ".join(mw.build_summary(now=NOW)["insights"])
        assert "1 posisi menang siap di-redeem senilai $30" in tips
        assert "1 posisi berakhir dalam 24 jam" in tips
        assert "80% nilai posisi ada di satu market (Hong Kong 33°C)" in tips
        assert "1 posisi turun lebih dari 50%" in tips and "1 posisi di harga ≥95¢" in tips
        assert "1 posisi kalah (harga 0)" in tips

    def test_not_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "POLYMARKET_WALLET_ADDRESS", None)
        assert mw.get_summary() is None


class TestClobReadOnly:

    def test_signature_matches_l2_spec(self):
        expected = base64.urlsafe_b64encode(hmac.new(base64.urlsafe_b64decode(SECRET), b"1700000000GET/data/orders",
                                                     hashlib.sha256).digest()).decode()
        assert mw.l2_signature(SECRET, "1700000000", "GET", "/data/orders") == expected

    def test_get_request_signed_over_path_without_query(self, with_keys):
        captured = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"balance": "12345678"}).encode()

        def fake_urlopen(req, timeout=None):
            captured.update(url=req.full_url, method=req.get_method(), headers=dict(req.header_items()))
            return Resp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen), patch("time.time", return_value=1700000000):
            assert mw.fetch_cash_balance() == 12.35
        assert captured["method"] == "GET"
        assert captured["url"].startswith("https://clob.polymarket.com/balance-allowance?asset_type=COLLATERAL")
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        assert headers["poly_api_key"] == "key-1" and headers["poly_passphrase"] == "pass-1"
        assert headers["poly_address"] == ME and headers["poly_timestamp"] == "1700000000"
        assert headers["poly_signature"] == mw.l2_signature(SECRET, "1700000000", "GET", "/balance-allowance")

    def test_open_orders_and_cash_in_summary(self, with_keys):
        def fake_clob(path, params=None):
            if path == "/balance-allowance":
                return {"balance": "50000000"}
            return {"data": [{"id": "o1", "side": "BUY", "outcome": "Yes", "price": "0.4", "original_size": "100",
                              "size_matched": "25", "market": "0xm"}], "next_cursor": "LTE="}

        with patch.object(mw, "_clob_get", side_effect=fake_clob):
            s = mw.build_summary(now=NOW)
        assert s["cash"] == 50.0 and s["open_orders"][0]["remaining_usdc"] == 30.0
        assert any("1 open order; $30 tertahan di order BUY" in t for t in s["insights"])

    def test_clob_failure_is_soft(self, with_keys):
        with patch.object(mw, "_clob_get", side_effect=OSError("down")):
            assert mw.fetch_cash_balance() is None and mw.fetch_open_orders() is None


class TestTelegram:

    def _msg(self, text):
        return handle_incoming_message(text, sender_chat_id="1", allowed_chat_id="1")

    def test_porto_summary(self, configured):
        text = self._msg("/porto")
        assert "Portfolio Polymarket" in text and "read-only" in text and "Nilai posisi: *$105.00*" in text
        assert "PnL: hari ini +$4 · 7 hari -$8 · 30 hari +$42 · all-time +$120" in text
        assert "Cash: - (butuh API key)" in text and "siap di-redeem: 1" in text and "💡" in text

    def test_porto_sections(self, configured):
        assert "Hong Kong 33°C" in self._msg("/porto posisi")
        assert "TRADE BUY Yes" in self._msg("/porto aktivitas")
        assert "butuh API key CLOB" in self._msg("/porto order")

    def test_porto_not_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "POLYMARKET_WALLET_ADDRESS", None)
        assert "POLYMARKET_WALLET_ADDRESS" in self._msg("/porto")

    def test_help(self):
        assert "/porto" in self._msg("/help")


def test_api(configured, monkeypatch):
    from fastapi.testclient import TestClient
    from app.dashboard import app
    client = TestClient(app)
    data = client.get("/api/my-wallet").json()
    assert data["configured"] is True and data["summary"]["claimable"]["count"] == 1
    assert 'id="myWalletContainer"' in client.get("/").text
    monkeypatch.setattr(settings, "POLYMARKET_WALLET_ADDRESS", None)
    assert client.get("/api/my-wallet").json() == {"configured": False, "summary": None}


def test_past_end_dates_not_counted_as_ending_soon(configured):
    summary = {"claimable": {"count": 0}, "lost": {"count": 0}, "open_orders": None, "stats": None,
               "positions": [mw._position_row(position("Old", 0.5, 5, 5, 0, 0, hours_left=-5))]}
    assert not any("berakhir dalam 24 jam" in t for t in mw.insights(summary, now=NOW))
