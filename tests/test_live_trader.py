"""
Test live trading (uang asli): aktivasi, batas harga, pengaman risiko, pencatatan & hasil, command & API.
Klien Polymarket selalu dipalsukan — test tidak pernah mengirim order sungguhan.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.core.database import get_db_session
from app.paper_trading import live_trader as lt
from app.paper_trading.models import LiveOrder
from app.paper_trading.telegram_bot import handle_incoming_message

NOW = datetime.now(timezone.utc).replace(microsecond=0)
KEY = "0x" + "ab" * 32


def decision(key="btc|0xm1", strategy="btc", prob=0.80, book=0.62, outcome="UP"):
    return {"key": key, "strategy": strategy, "market_id": key.split("|")[1], "title": "Bitcoin Up or Down - test",
            "side": "YES", "outcome": outcome, "prob": prob, "price": book + 0.01, "book_price": book,
            "fee": 0.016, "token": "tok-up", "detail": "BTC 84,000 (+0.12% dari open)"}


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(settings, "LIVE_TRADING", True)
    monkeypatch.setattr(settings, "POLY_PRIVATE_KEY", KEY)
    monkeypatch.setattr(settings, "POLY_FUNDER_ADDRESS", "0x" + "f" * 40)
    monkeypatch.setattr(settings, "POLY_SIGNATURE_TYPE", 1)
    monkeypatch.setattr(settings, "LIVE_ORDER_USD", 2.0)
    monkeypatch.setattr(settings, "LIVE_MAX_DAILY_USD", 5.0)
    monkeypatch.setattr(settings, "LIVE_MAX_OPEN_USD", 10.0)
    monkeypatch.setattr(settings, "LIVE_MAX_DAILY_LOSS", 3.0)
    monkeypatch.setattr(settings, "LIVE_DRY_RUN", False)
    monkeypatch.setattr("app.paper_trading.live_market_data.fetch_fee_rate", lambda token: 0.07)
    lt.set_switch(True)
    state = {"orders": [], "balance": 50.0, "resp": None, "sent": []}

    def fake_buy(token, usd, max_price):
        state["orders"].append((token, usd, max_price))
        return state["resp"] or {"success": True, "status": "matched", "orderID": "0xorder",
                                 "makingAmount": str(usd), "takingAmount": str(round(usd / 0.63, 4))}

    monkeypatch.setattr(lt, "place_fok_buy", fake_buy)
    monkeypatch.setattr(lt, "usdc_balance", lambda: state["balance"])
    db = get_db_session()
    db.query(LiveOrder).delete()
    db.commit()
    db.close()
    with patch("app.paper_trading.telegram.send_telegram_message",
               side_effect=lambda text, **kw: state["sent"].append(text) or {"success": True}):
        yield state


def test_inactive_by_default_and_config_problems(monkeypatch):
    monkeypatch.setattr(settings, "LIVE_TRADING", False)
    assert not lt.is_active() and lt.live_execute(decision()) is None
    monkeypatch.setattr(settings, "LIVE_TRADING", True)
    monkeypatch.setattr(settings, "POLY_PRIVATE_KEY", None)
    assert "POLY_PRIVATE_KEY belum diisi" in lt.config_problems()
    monkeypatch.setattr(settings, "POLY_PRIVATE_KEY", KEY)
    monkeypatch.setattr(settings, "POLY_FUNDER_ADDRESS", None)
    monkeypatch.setattr(settings, "POLY_SIGNATURE_TYPE", 1)
    assert any("POLY_FUNDER_ADDRESS" in p for p in lt.config_problems())
    monkeypatch.setattr(settings, "POLY_SIGNATURE_TYPE", 0)
    monkeypatch.setattr(settings, "LIVE_ORDER_USD", 500.0)
    assert any("melebihi batas" in p for p in lt.config_problems())  # batas $25 / $100 di kode


def test_max_price_respects_slippage_and_edge(monkeypatch):
    monkeypatch.setattr(settings, "LIVE_MAX_SLIPPAGE", 0.02)
    # ask 62¢ + 2¢ = 64¢; edge 80% − (64¢ + fee ≈1.6¢) ≈ 14¢ ≥ 5¢ → 64¢
    assert lt.max_price_for(0.80, 0.62, 0.07) == 0.64
    # peluang 70%: harga diturunkan sampai edge ≥ 5¢ → 63¢ (70 − 63 − 1.6 = 5.4¢)
    assert lt.max_price_for(0.70, 0.62, 0.07) == 0.63
    # tidak ada harga ≥ 30¢ yang edge-nya cukup
    assert lt.max_price_for(0.33, 0.30, 0.07) is None


def test_fills_records_and_notifies(live):
    row = lt.live_execute(decision(), NOW)
    assert row.status == "filled" and live["orders"] == [("tok-up", 2.0, 0.64)]
    assert float(row.spent) == 2.0 and float(row.avg_price) == pytest.approx(0.63, abs=1e-4)
    assert live["sent"][-1].startswith("💵 LIVE BUY (uang asli) · 🟠 BTC · 1 JAM")
    assert "(batas 64¢)" in live["sent"][-1]
    assert lt.live_execute(decision(), NOW) is None  # satu order per market
    assert len(live["orders"]) == 1


def test_only_configured_strategies_and_switch(live, monkeypatch):
    assert lt.live_execute(decision(key="btc15|0xm2", strategy="btc15"), NOW) is None  # bukan seri 1 jam
    lt.set_switch(False)
    assert lt.live_execute(decision(key="eth|0xm3", strategy="eth"), NOW) is None
    lt.set_switch(True)
    assert lt.live_execute(decision(key="eth|0xm3", strategy="eth"), NOW).status == "filled"
    assert "🔷 ETH · 1 JAM" in live["sent"][-1]


def test_daily_budget_balance_and_rejection(live):
    lt.live_execute(decision(key="btc|0xa"), NOW)
    lt.live_execute(decision(key="btc|0xb"), NOW)
    assert lt.live_execute(decision(key="btc|0xc"), NOW) is None  # $4 + $2 > $5/hari
    assert any("batas belanja live harian" in t for t in live["sent"])
    db = get_db_session()
    db.query(LiveOrder).delete()
    db.commit()
    db.close()
    live["balance"] = 1.0
    assert lt.live_execute(decision(key="btc|0xd"), NOW) is None
    assert any("saldo USDC $1.00 kurang" in t for t in live["sent"])
    live["balance"] = 50.0
    live["resp"] = {"success": False, "errorMsg": "order couldn't be fully filled. FOK orders are fully filled or killed."}
    row = lt.live_execute(decision(key="btc|0xe"), NOW)
    assert row.status == "rejected" and "fully filled" in row.error


def test_order_error_is_redacted(live, monkeypatch):
    def boom(token, usd, max_price):
        raise RuntimeError(f"signing failed with key {KEY}")
    monkeypatch.setattr(lt, "place_fok_buy", boom)
    row = lt.live_execute(decision(key="btc|0xerr"), NOW)
    assert row.status == "error" and KEY not in row.error and "***" in row.error
    assert all(KEY not in t for t in live["sent"])


def test_results_and_daily_loss_stop(live, monkeypatch):
    lt.live_execute(decision(key="btc|0xw"), NOW)
    lt.live_execute(decision(key="btc|0xl", outcome="DOWN"), NOW)
    monkeypatch.setattr("app.paper_trading.insider.market_info",
                        lambda ids: {i: {"winner": "YES"} for i in ids})  # Up menang
    assert lt.track_results(NOW + timedelta(hours=1)) == 2
    s = lt.live_summary(NOW)
    results = {o["outcome"]: (o["result"], round(o["pnl"], 2)) for o in s["orders"]}
    assert results["UP"] == ("WIN", round(2 / 0.63 - 2, 2)) and results["DOWN"] == ("LOSS", -2.0)
    assert s["totals"]["wins"] == 1 and s["totals"]["decided"] == 2
    # rugi terealisasi hari ini −2 + 1.17; tambah kekalahan → stop rugi harian $3
    db = get_db_session()
    db.add(LiveOrder(decision_key="live|x", strategy="btc", market_id="0xz", token_id="t", outcome="UP", usd=Decimal("2"),
                     max_price=Decimal("0.6"), status="filled", spent=Decimal("3"), shares=Decimal("5"), result="LOSS",
                     pnl=Decimal("-3"), local_day=lt._local_day(NOW), created_at=NOW))
    db.commit()
    db.close()
    assert lt.live_execute(decision(key="btc|0xafter"), NOW) is None
    assert any("stop harian live" in t for t in live["sent"])


def test_dry_run_does_not_send(live, monkeypatch):
    monkeypatch.setattr(settings, "LIVE_DRY_RUN", True)
    row = lt.live_execute(decision(key="btc|0xdry"), NOW)
    assert row.status == "dry_run" and live["orders"] == []
    assert live["sent"][-1].startswith("🧪 LIVE DRY RUN")


def test_commands_and_api(live):
    text = handle_incoming_message("/live", sender_chat_id="1", allowed_chat_id="1")
    assert "Live trading (uang asli)* — 🟢 AKTIF" in text and "Saldo USDC: $50.00" in text and KEY not in text
    assert "DIJEDA" in handle_incoming_message("/livestop", sender_chat_id="1", allowed_chat_id="1")
    assert lt.switch_on() is False
    handle_incoming_message("/livestart", sender_chat_id="1", allowed_chat_id="1")
    assert lt.switch_on() is True
    assert "/live" in handle_incoming_message("/help", sender_chat_id="1", allowed_chat_id="1")
    from fastapi.testclient import TestClient
    from app.dashboard import app
    client = TestClient(app)
    data = client.get("/api/live").json()
    assert data["active"] is True and KEY not in str(data)
    assert client.post("/api/live/stop").json()["switch_on"] is False
    assert client.post("/api/live/start").json()["switch_on"] is True
    assert 'id="liveContainer"' in client.get("/autobot").text


def test_btc_tick_triggers_live_for_hourly(live, monkeypatch):
    from app.paper_trading import autotrader as at
    from app.market_collector.collector import record_market_observations
    from app.paper_service import deposit_paper_funds
    deposit_paper_funds(Decimal("100"))
    hour = NOW.replace(minute=0, second=0)
    now = hour + timedelta(minutes=45)
    record_market_observations([{
        "market_id": "0xlive", "market_name": "Bitcoin Up or Down - live", "category": "Crypto", "status": "open",
        "is_resolved": False, "resolution_time": now + timedelta(minutes=15), "end_date": now + timedelta(minutes=15),
        "price_yes": Decimal("0.6"), "price_no": Decimal("0.4"), "current_price": Decimal("0.6"),
        "outcome_yes_label": "Up", "outcome_no_label": "Down", "timestamp": now}], now=now)
    monkeypatch.setattr(at, "_btc_market", lambda h, series="btc": {"condition_id": "0xlive", "title": "BTC live",
                                                                     "up": "tu", "down": "td", "accepting": True, "slug": "s"})
    monkeypatch.setattr(at, "_btc_klines", lambda symbol="BTCUSDT": [])
    monkeypatch.setattr(at, "btc_model", lambda k, h, n, d=60: {"p_up": 0.80, "price": 1, "open": 1, "change_pct": 0.2,
                                                                "minutes_left": 15})
    books = {"tu": {"price": 0.63, "book_price": 0.62, "fee": 0.016, "spread": 0.01, "shares": 8, "slippage": 0.01},
             "td": {"price": 0.39, "book_price": 0.38, "fee": 0.017, "spread": 0.01, "shares": 12, "slippage": 0.01}}
    monkeypatch.setattr(at, "_book_side", lambda token, usd: books[token])
    assert at.btc_tick(now) is not None
    assert live["orders"] == [("tu", 2.0, 0.64)]  # token Up, batas = ask buku 62¢ + 2¢


def test_live_config_editable_with_hard_cap(live, monkeypatch):
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_USD", 10.0)
    try:
        cfg = lt.set_live_config({"STRATEGIES": ["btc", "eth15", "btc5"], "ORDER_USD": 3, "MAX_SLIPPAGE": 0.01,
                                  "AUTO_CLAIM": False})
        assert cfg["STRATEGIES"]["value"] == "btc,eth15,btc5" and lt.live_strategies() == ["btc", "eth15", "btc5"]
        assert lt.lcfg("ORDER_USD") == 3.0 and lt.lcfg("AUTO_CLAIM") is False and cfg["ORDER_USD"]["max"] == 10.0
        with pytest.raises(ValueError, match="maksimal \\$10"):
            lt.set_live_config({"ORDER_USD": 50})  # tidak bisa melebihi LIVE_MAX_ORDER_USD di .env
        with pytest.raises(ValueError, match="tidak dikenal"):
            lt.set_live_config({"STRATEGIES": "btc,doge"})
        with pytest.raises(ValueError):
            lt.set_live_config({"LIVE_TRADING": True})  # saklar utama hanya dari .env
        assert lt.max_price_for(0.80, 0.62, 0.07) == 0.63  # slippage maks 1¢ dari dashboard
        from fastapi.testclient import TestClient
        from app.dashboard import app
        client = TestClient(app)
        assert client.put("/api/live/config", json={"updates": {"ORDER_USD": 99}}).status_code == 400
        data = client.put("/api/live/config", json={"updates": {"MAX_DAILY_USD": 7}}).json()
        assert data["limits"]["max_daily_usd"] == 7.0 and data["config"]["MAX_DAILY_USD"]["overridden"] is True
        assert client.delete("/api/live/config").json()["config"]["MAX_DAILY_USD"]["overridden"] is False
    finally:
        lt.reset_live_config()


def test_live_runs_for_selected_series_even_if_paper_strategy_off(live, monkeypatch):
    from app.paper_trading import autotrader as at
    lt.set_live_config({"STRATEGIES": ["eth15"]})
    try:
        start = NOW.replace(minute=(NOW.minute // 15) * 15, second=0)
        now = start + timedelta(minutes=10)
        monkeypatch.setattr(at, "_btc_market", lambda s, series="btc": {"condition_id": "0xe15", "title": "ETH 15m",
                                                                         "up": "tu", "down": "td", "accepting": True, "slug": "s"})
        monkeypatch.setattr(at, "_btc_klines", lambda symbol="BTCUSDT": [])
        monkeypatch.setattr(at, "btc_model", lambda k, s, n, d=60: {"p_up": 0.80, "price": 2400, "open": 2390,
                                                                    "change_pct": 0.4, "minutes_left": 5})
        books = {"tu": {"price": 0.63, "book_price": 0.62, "fee": 0.016, "spread": 0.01, "shares": 8},
                 "td": {"price": 0.39, "book_price": 0.38, "fee": 0.017, "spread": 0.01, "shares": 12}}
        monkeypatch.setattr(at, "_book_side", lambda token, usd: books[token])
        assert at.btc_tick(now, series="eth15", shadow=True) is None  # paper eth15 nonaktif: tidak beli paper
        assert live["orders"] == [("tu", 2.0, 0.64)]                  # tapi live eth15 tetap jalan
        assert "🔷 ETH · 15 MENIT" in live["sent"][-1]
    finally:
        lt.reset_live_config()


def test_redeem_call_data_encodes_conditional_tokens_call():
    cid = "0x" + "ab" * 32
    to, data = lt.redeem_call_data(cid)
    assert to == "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # ConditionalTokens Polygon (config py-clob-client)
    assert data.startswith("0x01b7037c")                         # redeemPositions(address,bytes32,bytes32,uint256[])
    assert "ab" * 32 in data and data.endswith(("0" * 63 + "1") + ("0" * 63 + "2"))  # indexSets [1, 2]


def test_state_keys_fit_column():
    key = lt._state_key("live_claim", "0x" + "ab" * 32)
    assert len(key) <= 50 and key.startswith("live_claim:")


def test_auto_claim_submits_once_and_marks_orders(live, monkeypatch):
    monkeypatch.setattr(settings, "POLY_BUILDER_API_KEY", "k")
    monkeypatch.setattr(settings, "POLY_BUILDER_SECRET", "s")
    monkeypatch.setattr(settings, "POLY_BUILDER_PASSPHRASE", "p")
    lt.live_execute(decision(key="btc|0xcw"), NOW)
    positions = {"0xcw": {"title": "Bitcoin Up or Down - won", "value": 3.17, "shares": 3.17},
                 "0xother": {"title": "Manual win", "value": 1.0, "shares": 1.0}}
    monkeypatch.setattr(lt, "redeemable_positions", lambda: positions)
    calls = []
    monkeypatch.setattr(lt, "submit_redeem", lambda cids: calls.append(list(cids)) or {"transaction_id": "id1",
                                                                                         "transaction_hash": "0x" + "c" * 64})
    assert sorted(lt.auto_claim(NOW)) == ["0xcw", "0xother"]
    assert calls == [["0xcw", "0xother"]]
    assert lt.auto_claim(NOW + timedelta(minutes=5)) == []      # jangan kirim ulang selagi menunggu
    assert lt.auto_claim(NOW + timedelta(minutes=31)) != []     # masih redeemable setelah 30 menit: coba lagi
    text = next(t for t in live["sent"] if t.startswith("🪙 AUTO CLAIM"))
    assert "2 market · ±$4.17" in text and "polygonscan.com/tx/0x" in text
    assert lt.live_summary()["orders"][0]["claimed"] is True


def test_auto_claim_needs_builder_creds_and_proxy_account(live, monkeypatch):
    monkeypatch.setattr(settings, "POLY_BUILDER_API_KEY", None)
    assert any("Builder API" in p for p in lt.claim_problems()) and lt.auto_claim(NOW) == []
    monkeypatch.setattr(settings, "POLY_BUILDER_API_KEY", "k")
    monkeypatch.setattr(settings, "POLY_BUILDER_SECRET", "s")
    monkeypatch.setattr(settings, "POLY_BUILDER_PASSPHRASE", "p")
    monkeypatch.setattr(settings, "POLY_SIGNATURE_TYPE", 0)
    assert any("tipe 1" in p for p in lt.claim_problems())


def test_live_calendar_and_trades(live, monkeypatch):
    lt.live_execute(decision(key="btc|0xk1"), NOW)
    lt.live_execute(decision(key="eth|0xk2", strategy="eth", outcome="DOWN"), NOW)
    monkeypatch.setattr("app.paper_trading.insider.market_info", lambda ids: {i: {"winner": "YES"} for i in ids})
    resolved = NOW + timedelta(minutes=20)
    lt.track_results(resolved)
    from zoneinfo import ZoneInfo as _Z
    local = resolved.astimezone(_Z(settings.NOTIFY_TIMEZONE))
    cal = lt.live_calendar(f"{local.year:04d}-{local.month:02d}")
    day = cal["days"][local.date().isoformat()]
    assert day["trades"] == 2 and day["wins"] == 1 and day["pnl"] == round(2 / 0.63 - 2 - 2, 2)
    assert lt.live_calendar(f"{local.year:04d}-{local.month:02d}", strategy="eth_all")["totals"]["trades"] == 1
    assert lt.live_calendar_year(local.year)["totals"]["trades"] == 2
    trades = lt.live_closed(day=local.date().isoformat())
    assert {t["result"] for t in trades} == {"MENANG", "KALAH"} and trades[0]["detail"].startswith("💵 live")
    from fastapi.testclient import TestClient
    from app.dashboard import app
    client = TestClient(app)
    data = client.get("/api/autotrade/calendar", params={"month": f"{local.year:04d}-{local.month:02d}", "source": "live"}).json()
    assert data["source"] == "live" and data["totals"]["trades"] == 2
    assert len(client.get("/api/autotrade/calendar/trades", params={"date": local.date().isoformat(), "source": "live"}).json()["trades"]) == 2
    assert client.get("/api/autotrade/calendar", params={"source": "moon"}).status_code == 400
    assert 'id="calSrcLive"' in client.get("/autobot").text
