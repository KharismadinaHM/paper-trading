"""Test insider wallet: pengelompokan trade, filter kandidat, skor, alert, pelacakan hasil, command & API."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.core.database import get_db_session
from app.paper_trading import insider as ins
from app.paper_trading import live_market_data as live
from app.paper_trading.models import InsiderFlag
from app.paper_trading.telegram_bot import handle_incoming_message

NOW = datetime.now(timezone.utc).replace(microsecond=0)
TS = int(NOW.timestamp())
NEW = "0x" + "1" * 40
OLD = "0x" + "2" * 40


def tr(wallet, cash, price, title="Will X announce Y by Friday?", cond="0xcond", side="BUY", idx=0, outcome="Yes",
       ts=None, name="Scary-Pearl"):
    return {"proxyWallet": wallet, "side": side, "conditionId": cond, "size": cash / price, "price": price,
            "timestamp": ts or TS - 60, "title": title, "eventSlug": "x-announce-y", "outcome": outcome,
            "outcomeIndex": idx, "name": name, "pseudonym": "Scary-Pearl"}


class FakeApi:
    def __init__(self):
        self.trades = [tr(NEW, 6000, 0.12), tr(NEW, 6500, 0.14, ts=TS - 30),            # wallet baru, longshot besar
                       tr(OLD, 3000, 0.20, cond="0xc2"),                                  # wallet lama, banyak market
                       tr(NEW, 9000, 0.95, cond="0xc3"),                                  # favorit 95¢: bukan kandidat
                       tr(NEW, 5000, 0.10, cond="0xc4", title="Spirit vs ShindeN (BO3)"),  # olahraga/esports
                       tr(NEW, 5000, 0.10, cond="0xc5", side="SELL")]
        self.profiles = {NEW: {"traded": 2, "first": TS - 86400, "value": 13000},
                         OLD: {"traded": 400, "first": TS - 400 * 86400, "value": 900000}}

    def __call__(self, path, **params):
        user = params.get("user")
        if path == "/trades":
            return self.trades
        if path == "/traded":
            return {"traded": self.profiles[user]["traded"]}
        if path == "/activity":
            return [{"timestamp": self.profiles[user]["first"]}]
        if path == "/value":
            return [{"value": self.profiles[user]["value"]}]
        raise AssertionError(path)


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr("app.paper_trading.wallets._get", fake)
    monkeypatch.setattr(ins, "market_info", lambda ids: {c: {"end": NOW + timedelta(hours=20), "closed": False,
                                                             "winner": None} for c in ids})
    live.clear_cache()
    db = get_db_session()
    db.query(InsiderFlag).delete()
    db.commit()
    db.close()
    yield fake
    live.clear_cache()


def test_group_and_candidate_filter(api):
    bets = ins.group_bets(api.trades)
    new_bet = next(b for b in bets if b["wallet"] == NEW and b["condition_id"] == "0xcond")
    assert new_bet["cash"] == pytest.approx(12500) and new_bet["avg_price"] == pytest.approx(12500 / (50000 + 6500 / 0.14))
    assert not any(b["condition_id"] == "0xc5" for b in bets)  # SELL dilewati
    candidates = {b["condition_id"] for b in bets if ins.is_candidate(b)}
    assert candidates == {"0xcond", "0xc2"}  # 95¢ dan esports tersaring


def test_score_new_wallet_longshot(api):
    bet = next(b for b in ins.group_bets(api.trades) if b["condition_id"] == "0xcond")
    profile = ins.wallet_profile(NEW, NOW)
    assert profile["markets_traded"] == 2 and profile["age_days"] == pytest.approx(1.0, abs=0.01)
    score, reasons = ins.score_bet(bet, profile, NOW + timedelta(hours=20), NOW)
    # baru 1 hari (3) + 2 market (3) + 13¢ (3) + $12.5k (2) + 96% porto (2) + selesai 20 jam (1)
    assert score == 14
    assert "baru 2 market" in reasons and "taruhan besar $12,500" in reasons
    assert any(r.startswith("market selesai dalam 20 jam") for r in reasons)


def test_scan_flags_alerts_once_and_tracks_result(api, monkeypatch):
    sent = []
    with patch("app.paper_trading.telegram.send_telegram_message",
               side_effect=lambda text, **kw: sent.append((text, kw)) or {"success": True}):
        ins.scan(NOW)
        ins.scan(NOW)  # tidak dobel
    assert len(sent) == 1
    text, kw = sent[0]
    assert text.startswith("🕵️ WALLET MENCURIGAKAN · skor 14/15 · Scary-Pearl")
    assert "Beli Yes — Will X announce Y by Friday?" in text and "$12,500 @ 13.0¢" in text
    assert "Heuristik, bukan bukti" in text
    assert kw["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"wf:{NEW}"
    # wallet lama (skor rendah) tidak ditandai
    db = get_db_session()
    assert [f.wallet for f in db.query(InsiderFlag).all()] == [NEW]
    db.close()

    monkeypatch.setattr(ins, "market_info", lambda ids: {c: {"winner": "YES", "closed": True, "end": None} for c in ids})
    assert ins.track_results(NOW + timedelta(days=1)) == 1
    report = ins.insider_report()
    assert report["summary"]["wins"] == 1 and report["summary"]["decided"] == 1
    assert report["summary"]["roi"] == pytest.approx((1 - 0.13) / 0.13, rel=0.05)
    assert report["items"][0]["result"] == "WIN"


def test_outcome_index_decides_result(api, monkeypatch):
    with patch("app.paper_trading.telegram.send_telegram_message", return_value={"success": True}):
        ins.scan(NOW)
    monkeypatch.setattr(ins, "market_info", lambda ids: {c: {"winner": "NO", "closed": True, "end": None} for c in ids})
    ins.track_results(NOW + timedelta(days=1))
    assert ins.insider_report()["items"][0]["result"] == "LOSS"  # bertaruh outcome index 0, yang menang index 1


def test_command_and_api(api):
    with patch("app.paper_trading.telegram.send_telegram_message", return_value={"success": True}):
        ins.scan(NOW)
    text = handle_incoming_message("/insider 24", sender_chat_id="1", allowed_chat_id="1")
    assert "Insider wallet* (24 jam terakhir)" in text and "14/15" in text and "Scary-Pearl" in text
    assert "/insider" in handle_incoming_message("/help", sender_chat_id="1", allowed_chat_id="1")
    from fastapi.testclient import TestClient
    from app.dashboard import app
    data = TestClient(app).get("/api/insider").json()
    assert data["summary"]["flags"] == 1 and data["max_score"] == 15 and data["items"][0]["wallet"] == NEW
    assert 'id="insiderBody"' in TestClient(app).get("/").text


def test_disabled(api, monkeypatch):
    monkeypatch.setattr(settings, "INSIDER_ENABLED", False)
    assert ins.run_insider_scan() == 0
