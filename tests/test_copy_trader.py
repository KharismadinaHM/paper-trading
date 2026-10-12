"""Test copy trade (paper) wallet yang diikuti: pengaturan, beli, lewati, ikut jual, polling, perintah & API."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.core.database import get_db_session
from app.market_collector.collector import record_market_observations
from app.paper_service import deposit_paper_funds, get_open_positions
from app.paper_trading import autotrader as at, copy_trader as ct
from app.paper_trading.models import AutotradeDecision, TrackedWallet
from app.paper_trading.telegram_bot import handle_incoming_message

NOW = datetime.now(timezone.utc).replace(microsecond=0)
WALLET = "0x" + "ab" * 20
MARKET = "0xmarketcopy"


def seed(price="0.45"):
    record_market_observations([{
        "market_id": MARKET, "market_name": "Will it rain in London?", "category": "Weather", "status": "open",
        "is_resolved": False, "resolution_time": NOW + timedelta(days=1), "end_date": NOW + timedelta(days=1),
        "price_yes": Decimal(price), "price_no": Decimal("1") - Decimal(price), "current_price": Decimal(price),
        "outcome_yes_label": "Yes", "outcome_no_label": "No", "timestamp": NOW,
    }])


def row(side="BUY", price=0.45, size=100.0, ts=None, tx="0xtx1", oi=0):
    return {"proxyWallet": WALLET, "timestamp": int((ts or NOW).timestamp()), "conditionId": MARKET, "type": "TRADE",
            "size": size, "usdcSize": size * price, "transactionHash": tx, "price": price, "asset": "tok-yes",
            "side": side, "outcomeIndex": oi, "title": "Will it rain in London?", "outcome": "Yes" if oi == 0 else "No",
            "eventSlug": "rain-london"}


@pytest.fixture
def env(monkeypatch):
    deposit_paper_funds(Decimal("500"))
    seed()
    db = get_db_session()
    db.add(TrackedWallet(address=WALLET, name="RainKing", status="tracking", follow=True, source="manual", added_at=NOW))
    db.commit()
    db.close()
    at.set_enabled(True)
    at.set_config({"STRATEGIES": ["copy"]})
    book = {"price": 0.46}
    monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": book["price"], "fee": 0.0, "spread": 0.02,
                                                               "shares": usd / book["price"], "slippage": 0.01})
    sent = []
    with patch("app.paper_trading.telegram.send_telegram_message",
               side_effect=lambda text, **kw: sent.append(text) or {"success": True}):
        yield {"book": book, "sent": sent}


def wallet():
    db = get_db_session()
    try:
        return db.get(TrackedWallet, WALLET)
    finally:
        db.close()


def test_settings_validate_and_enable_follows(env):
    assert ct.get_settings(WALLET)["enabled"] is False and ct.get_settings(WALLET)["usd"] == float(at.cfg("ORDER_USD"))
    c = ct.set_settings(WALLET, {"enabled": True, "usd": 3, "max_slippage": 0.02})
    assert c["enabled"] and c["usd"] == 3 and c["max_slippage"] == 0.02
    with pytest.raises(ValueError):
        ct.set_settings(WALLET, {"usd": 0})
    with pytest.raises(ValueError):
        ct.set_settings(WALLET, {"min_price": 0.9, "max_price": 0.5})


def test_buy_is_copied_once_with_fresh_price(env):
    ct.set_settings(WALLET, {"enabled": True, "usd": 2})
    results = ct.process_wallet_trades(wallet(), [row(tx="a"), row(tx="b", size=50)], NOW)
    assert [r["action"] for r in results] == ["bought"]                     # dua fill = satu order
    pos = [p for p in get_open_positions() if p["market_id"] == MARKET]
    assert pos and pos[0]["strategy_version"] == "auto_copy_v1" and str(pos[0]["side"]).endswith("YES")
    assert any("👛 COPY TRADE" in m and "Salin 👛 RainKing" in m for m in env["sent"])
    assert ct.process_wallet_trades(wallet(), [row(tx="c")], NOW) == []     # market + outcome yang sama: sekali saja
    assert ct.copy_stats()[WALLET]["trades"] == 1 and ct.copy_stats()[WALLET]["open"] == 1


def test_skips_when_price_ran_old_or_disabled(env):
    ct.set_settings(WALLET, {"enabled": True, "max_slippage": 0.01})
    env["book"]["price"] = 0.52
    r = ct.process_wallet_trades(wallet(), [row()], NOW)
    assert r[0]["action"] == "skipped" and "harga sudah naik 45.0¢ → 52.0¢" in r[0]["reason"]
    assert any("COPY DILEWATI" in m for m in env["sent"])
    db = get_db_session()
    assert db.query(AutotradeDecision).filter_by(strategy="copy", status="rejected").count() == 1
    db.close()
    old = ct.process_wallet_trades(wallet(), [row(ts=NOW - timedelta(hours=2), oi=1)], NOW)
    assert old[0]["action"] == "skipped" and "menit lalu" in old[0]["reason"]
    at.set_config({"STRATEGIES": ["btc"]})                                  # strategi copy tidak dicentang
    assert ct.process_wallet_trades(wallet(), [row(tx="z", oi=1)], NOW) == []
    ct.set_settings(WALLET, {"enabled": False})
    at.set_config({"STRATEGIES": ["copy"]})
    assert ct.process_wallet_trades(wallet(), [row(tx="y", oi=1)], NOW) == []


def test_mirror_sell_closes_copy_position(env):
    ct.set_settings(WALLET, {"enabled": True, "usd": 2})
    ct.process_wallet_trades(wallet(), [row()], NOW)
    later = NOW + timedelta(minutes=5)
    seed("0.60")
    with patch("app.paper_service.get_market_by_id", lambda market_id, now=None: {
            "market_id": MARKET, "status": "open", "is_resolved": False, "price_yes": Decimal("0.60"),
            "price_no": Decimal("0.40"), "current_price": Decimal("0.60"), "is_stale": False, "timestamp": later}):
        r = ct.process_wallet_trades(wallet(), [row(side="SELL", price=0.60, tx="s")], later)
    assert r[0]["action"] == "sold" and r[0]["result"]["realized_pnl"] > 0
    assert not [p for p in get_open_positions() if p["market_id"] == MARKET]
    assert any("COPY SELL" in m and "ikut dijual" in m for m in env["sent"])


def test_polling_triggers_copy_for_all_categories(env, monkeypatch):
    from app.paper_trading import wallets
    ct.set_settings(WALLET, {"enabled": True})
    monkeypatch.setattr(settings, "WALLET_ALERT_WEATHER_ONLY", True)
    w = wallet()
    db = get_db_session()
    db.get(TrackedWallet, WALLET).last_activity_ts = int(NOW.timestamp()) - 60
    db.commit()
    db.close()
    btc = {**row(tx="btc1"), "title": "Bitcoin Up or Down", "conditionId": MARKET}
    monkeypatch.setattr(wallets, "_get", lambda path, **params: [btc] if path == "/activity" else [])
    monkeypatch.setattr(wallets, "my_open_positions", lambda: {})
    wallets.poll_followed_wallets(NOW)
    assert ct.copy_stats()[WALLET]["trades"] == 1                         # bukan cuaca: tidak di-alert, tetap disalin
    wallets.poll_followed_wallets(NOW + timedelta(minutes=1))              # transaksi yang sama tidak diproses ulang
    assert ct.copy_stats()[WALLET]["trades"] == 1
    assert w.address == WALLET


def test_command_and_api(env):
    assert "Copy trade (paper)" in handle_incoming_message("/copy", "1")
    reply = handle_incoming_message(f"/copy {WALLET} on 4", "1")
    assert "AKTIF" in reply and "$4/trade" in reply
    assert "$5/trade" in handle_incoming_message(f"/copy {WALLET} usd 5", "1")
    assert "slip ≤2¢" in handle_incoming_message(f"/copy {WALLET} slip 2", "1")
    assert "tidak ikut jual" in handle_incoming_message(f"/copy {WALLET} jual off", "1")
    assert "/copy" in handle_incoming_message("/help", "1")
    from fastapi.testclient import TestClient
    from app.dashboard import app
    client = TestClient(app)
    data = client.get("/api/copy").json()
    assert data["active"] and data["wallets"][0]["copy"]["usd"] == 5
    assert client.put(f"/api/copy/{WALLET}", json={"enabled": False}).json()["copy"]["enabled"] is False
    assert client.put(f"/api/copy/{WALLET}", json={"usd": 0}).status_code == 400
    assert at.strategy_header("copy") == "👛 COPY TRADE"
