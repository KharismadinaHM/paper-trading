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
    at.set_enabled(False)          # auto bot mati: copy trade tetap jalan (terpisah)
    ct.set_active(True)
    book = {"price": 0.46}
    monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": book["price"], "fee": 0.0, "spread": 0.02,
                                                               "shares": usd / book["price"], "slippage": 0.01})
    sent, chats = [], []
    with patch("app.paper_trading.telegram.send_telegram_message",
               side_effect=lambda text, **kw: sent.append(text) or chats.append(kw.get("chat_id")) or {"success": True}):
        yield {"book": book, "sent": sent, "chats": chats}


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
    assert any("👛 COPY BUY (paper) · RainKing" in m and "Salin 👛 RainKing" in m for m in env["sent"])
    assert env["chats"] and all(c is None for c in env["chats"])        # chat utama, bukan grup auto bot
    assert at.today_summary()["trades"] == 0 and ct.today()["bought"] == 1  # tidak masuk batas/statistik auto bot
    assert at.trade_history() == [] and len(at.trade_history(strategy="copy")) == 1
    assert ct.process_wallet_trades(wallet(), [row(tx="c")], NOW) == []     # market + outcome yang sama: sekali saja
    assert ct.copy_stats()[WALLET]["trades"] == 1 and ct.copy_stats()[WALLET]["open"] == 1


def test_skips_when_price_ran_old_or_disabled(env):
    ct.set_settings(WALLET, {"enabled": True, "max_slippage": 0.01})
    env["book"]["price"] = 0.52
    r = ct.process_wallet_trades(wallet(), [row()], NOW)
    assert r[0]["action"] == "skipped" and "harga sudah naik 45.0¢ → 52.0¢" in r[0]["reason"]
    assert not any("COPY DILEWATI" in m for m in env["sent"])          # default: tidak dikirim satu per satu
    assert ct.skip_summary() == {"harga sudah lari": 1} and "harga sudah naik" in ct.format_skips()
    db = get_db_session()
    assert db.query(AutotradeDecision).filter_by(strategy="copy", status="rejected").count() == 1
    db.close()
    old = ct.process_wallet_trades(wallet(), [row(ts=NOW - timedelta(hours=2), oi=1)], NOW)
    assert old[0]["action"] == "skipped" and "menit lalu" in old[0]["reason"]
    ct.set_active(False)                                                   # saklar copy mati
    assert ct.process_wallet_trades(wallet(), [row(tx="z", oi=1)], NOW) == []
    ct.set_active(True)
    ct.set_settings(WALLET, {"enabled": False})
    assert ct.process_wallet_trades(wallet(), [row(tx="y", oi=1)], NOW) == []


def test_copy_has_its_own_risk_limits(env, monkeypatch):
    monkeypatch.setattr(settings, "COPY_MAX_DAILY_USD", 3.0)
    monkeypatch.setattr(settings, "COPY_NOTIFY_SKIPS", True)
    ct.set_settings(WALLET, {"enabled": True, "usd": 2})
    assert ct.process_wallet_trades(wallet(), [row()], NOW)[0]["action"] == "bought"
    r = ct.process_wallet_trades(wallet(), [row(tx="n", oi=1)], NOW)
    assert r[0]["action"] == "skipped" and "batas harian copy $3" in r[0]["reason"]
    assert any("COPY DILEWATI" in m for m in env["sent"])                 # notif per kejadian bila diaktifkan


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
    assert "dihentikan" in handle_incoming_message("/copy stop", "1") and not ct.is_active()
    assert "dijalankan" in handle_incoming_message("/copy start", "1") and ct.is_active()
    assert "Tidak ada copy yang dilewati" in handle_incoming_message("/copy dilewati", "1")
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
    assert data["active"] and data["wallets"][0]["copy"]["usd"] == 5 and data["limits"]["max_daily_usd"] > 0
    assert client.post("/api/copy/stop").json()["active"] is False and client.post("/api/copy/start").json()["active"]
    assert client.get("/api/copy-skips").json() == {"skips": []} and client.get("/api/copy-trades").json()["history"] == []
    assert 'id="walletRows"' in client.get("/copy").text and 'href="/copy"' in client.get("/autobot").text
    assert client.put(f"/api/copy/{WALLET}", json={"enabled": False}).json()["copy"]["enabled"] is False
    assert client.put(f"/api/copy/{WALLET}", json={"usd": 0}).status_code == 400
    assert "copy" not in at.get_config()["STRATEGIES"]["options"]        # bukan strategi auto bot lagi


def test_scalper_detection_and_clearer_book_reasons(env, monkeypatch):
    from app.paper_trading import wallets
    base = int(NOW.timestamp())
    acts = []
    for i in range(8):  # beli lalu jual aset yang sama ±1 menit kemudian (97¢ → 99¢)
        acts.append({"side": "BUY", "asset": f"a{i}", "timestamp": base - 3600 * i})
        acts.append({"side": "SELL", "asset": f"a{i}", "timestamp": base - 3600 * i + 60})
    monkeypatch.setattr(wallets, "_get", lambda path, **p: acts)
    style = ct.wallet_style(WALLET, refresh=True)
    assert style["scalper"] and style["scalp_ratio"] == 1.0 and style["median_hold_min"] == 1.0
    assert "scalper" in ct.format_copy_list()
    monkeypatch.setattr("app.paper_trading.live_market_data.fetch_order_books",
                        lambda tokens, ttl=None: {"tok": {"ask": None, "bid": 0.98, "asks": [], "bids": [(0.98, 50)]}})
    assert "ask kosong" in ct._book_problem("tok", 3)
    monkeypatch.setattr("app.paper_trading.live_market_data.fetch_order_books", lambda tokens, ttl=None: {})
    assert "tidak punya order book" in ct._book_problem("tok", 3)


def test_min_delay_three_minutes_and_fast_polling(env, monkeypatch):
    from app.paper_trading import autotrader as _at, wallets
    _at._set_state(f"copy|{WALLET}", '{"enabled": true, "max_delay_min": 1}')
    assert ct.get_settings(WALLET)["max_delay_min"] == 3           # nilai lama 1 menit dinaikkan ke batas minimum
    with pytest.raises(ValueError):
        ct.set_settings(WALLET, {"max_delay_min": 1})
    assert ct.copy_wallets() == [WALLET]
    polled = []
    monkeypatch.setattr(wallets, "poll_followed_wallets", lambda now=None, only=None: polled.append(only) or 0)
    wallets.run_copy_polling()
    assert polled == [[WALLET]]
    ct.set_active(False)
    wallets.run_copy_polling()
    assert polled == [[WALLET]]                                     # copy berhenti: tidak ada polling cepat
