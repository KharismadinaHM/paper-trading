"""Test verdict paper → live: jumlah trade, rentang hari, ROI, tanpa tiket lotre, kedua paruh, z."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.database import get_db_session
from app.paper_service import deposit_paper_funds
from app.paper_trading import autotrade_verdict as av
from app.paper_trading import autotrader as at
from app.paper_trading.models import PaperAccount, PaperTrade, PaperTradeStatus, TradeSide
from app.paper_trading.telegram_bot import handle_incoming_message

NOW = datetime(2031, 5, 30, tzinfo=timezone.utc)


def add_trades(version, pnls, start=NOW - timedelta(days=10), span_days=8.0):
    deposit_paper_funds(Decimal("1"))
    db = get_db_session()
    acc = db.query(PaperAccount).first().id
    for i, pnl in enumerate(pnls):
        at_ = start + timedelta(days=span_days * i / max(len(pnls) - 1, 1))
        db.add(PaperTrade(account_id=acc, market_id=f"0x{version}{i}", side=TradeSide.YES, entry_price=Decimal("0.5"),
                          position_size=Decimal("1"), shares=Decimal("2"), exit_price=Decimal("1" if pnl > 0 else "0"),
                          gross_pnl=Decimal(str(pnl)), net_pnl=Decimal(str(pnl)),
                          status=PaperTradeStatus.WON if pnl > 0 else PaperTradeStatus.LOST,
                          closed_at=at_, opened_at=at_, strategy_version=version))
    db.commit()
    db.close()


def test_not_enough_data():
    add_trades("auto_btc_v1", [0.9, -1.0] * 20)
    r = av.verdict(days=30, now=NOW)["series"]["btc"]
    assert r["verdict"] == "BELUM" and "jumlah trade" in r["reasons"]


def test_pass_when_consistent():
    add_trades("auto_btc5_v1", [0.9, 0.9, -1.0] * 60)  # 180 trade, 8 hari, ROI ≈ +27%, merata
    r = av.verdict(days=30, now=NOW)["series"]["btc5"]
    assert r["verdict"] == "LULUS" and r["roi"] > 0 and r["roi_without_top"] > 0 and r["n"] == 180


def test_lottery_profit_is_not_a_pass():
    pnls = [-1.0] * 170 + [0.9] * 5 + [40.0] * 5  # untung hanya karena 5 kemenangan raksasa
    add_trades("auto_eth15_v1", pnls)
    r = av.verdict(days=30, now=NOW)["series"]["eth15"]
    assert r["roi"] > 0 and r["roi_without_top"] < 0
    assert r["verdict"] in ("BELUM", "GAGAL") and r["verdict"] != "LULUS"


def test_clear_loss_fails_and_command():
    add_trades("auto_btc15_v1", [0.9, -1.0, -1.0] * 60)
    r = av.verdict(days=30, now=NOW)["series"]["btc15"]
    assert r["verdict"] == "GAGAL" and r["z"] <= -2
    at.set_stats_since(None)  # tanpa periode: semua trade
    text = handle_incoming_message("/autoverdict", sender_chat_id="1", allowed_chat_id="1")
    assert "Verdict paper → live" in text and "❌ *🟠 BTC · 15 MENIT (btc15): GAGAL*" in text
    assert "/autoverdict" in handle_incoming_message("/help", sender_chat_id="1", allowed_chat_id="1")



def test_api_and_page():
    from fastapi.testclient import TestClient
    from app.dashboard import app
    client = TestClient(app)
    data = client.get("/api/autotrade/verdict", params={"days": 3}).json()
    assert data["criteria"]["min_trades"] == 400 and "total" in data
    assert 'id="verdictBody"' in client.get("/autobot").text
