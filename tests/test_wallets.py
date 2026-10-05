"""
Test wallet tracker: statistik (win rate, PnL), discovery, track/follow/skip, alert transaksi,
command & tombol Telegram, dan API dashboard. Semua panggilan API Polymarket dipalsukan.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.paper_trading import live_market_data as live
from app.paper_trading import wallets as wl
from app.paper_trading.telegram_bot import handle_callback_query, handle_incoming_message

NOW = datetime.now(timezone.utc).replace(microsecond=0)
TS = int(NOW.timestamp())
A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40


def closed(pnl, hours_ago=5):
    return {"realizedPnl": pnl, "timestamp": TS - hours_ago * 3600, "title": "Highest temperature in Tokyo"}


def trade(ts, usdc=20.0, size=40.0, tx="0xt1", asset="as1", title="Will the highest temperature in Hong Kong be 33°C?",
          cond="0xc1", side="BUY", outcome="Yes", name="Alice"):
    return {"timestamp": ts, "usdcSize": usdc, "size": size, "transactionHash": tx, "asset": asset, "title": title,
            "conditionId": cond, "side": side, "outcome": outcome, "price": usdc / size, "name": name,
            "eventSlug": "highest-temperature-in-hong-kong", "type": "TRADE"}


class FakeApi:
    """Respons data-api per wallet; `calls` merekam path & parameter."""

    def __init__(self):
        self.closed = {A: [closed(10), closed(5), closed(-3)], B: [closed(1)] * 3, C: [closed(4)] * 20}
        self.positions = {A: [{"redeemable": True, "cashPnl": -8, "endDate": NOW.isoformat()},
                              {"redeemable": False, "currentValue": 12.5}]}
        self.activity = {A: [trade(TS - 600)], C: [trade(TS - 30 * 86400, name="Stale")]}
        self.board = [{"proxyWallet": A, "userName": "Alice", "rank": "1", "pnl": 500, "vol": 9000},
                      {"proxyWallet": B, "userName": B, "rank": "2", "pnl": 300, "vol": 4000},
                      {"proxyWallet": C, "userName": "Carol", "rank": "3", "pnl": 200, "vol": 1000}]
        self.calls = []

    def __call__(self, path, **params):
        self.calls.append((path, params))
        user = params.get("user")
        if path == "/closed-positions":
            return self.closed.get(user, [])[params.get("offset", 0):params.get("offset", 0) + 50]
        if path == "/positions":
            return self.positions.get(user, [])
        if path == "/activity":
            rows = self.activity.get(user, [])
            return [r for r in rows if r["timestamp"] >= params.get("start", 0)]
        if path == "/value":
            return [{"value": 99.0}]
        if path == "/v1/leaderboard":
            if user:
                row = next((r for r in self.board if r["proxyWallet"] == user), None)
                return [row] if row else []
            return self.board
        raise AssertionError(path)


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(wl, "_get", fake)
    monkeypatch.setattr(settings, "WALLET_DISCOVERY_MIN_RESOLVED", 3)
    live.clear_cache()
    yield fake
    live.clear_cache()


@pytest.fixture
def sent():
    messages = []
    with patch("app.paper_trading.telegram.send_telegram_message",
               side_effect=lambda text, **kw: messages.append(text) or {"success": True}):
        yield messages


class TestStats:

    @pytest.mark.parametrize("value", [A, A.upper().replace("0X", "0x"), f"https://polymarket.com/profile/{A}"])
    def test_normalize_address(self, value):
        assert wl.normalize_address(value) == A

    def test_invalid_address(self):
        with pytest.raises(wl.WalletError):
            wl.normalize_address("0x123")

    def test_win_rate_counts_unredeemed_losers(self, api):
        s = wl.compute_stats(A, now=NOW)
        # closed: +10 +5 −3, redeemable kalah: −8 → 2 menang / 4 selesai
        assert (s["wins"], s["losses"], s["resolved"], s["win_rate"]) == (2, 2, 4, 0.5)
        assert s["sample_pnl"] == 4 and s["pnl"] == 500  # PnL resmi leaderboard bulan ini
        assert s["open_positions"] == 1 and s["open_value"] == 12.5 and s["portfolio_value"] == 99.0
        assert s["name"] == "Alice" and s["weather_share"] == 1.0

    def test_closed_positions_sorted_by_time(self, api):
        wl.compute_stats(A, now=NOW)
        params = next(p for path, p in api.calls if path == "/closed-positions")
        assert params["sortBy"] == "TIMESTAMP" and params["sortDirection"] == "DESC"

    def test_truncated_sample_limits_redeemables_to_same_span(self, api):
        api.closed[A] = [closed(1, hours_ago=1)] * 300  # 6 halaman penuh, semuanya 1 jam lalu
        api.positions[A] = [{"redeemable": True, "cashPnl": -8, "endDate": (NOW - timedelta(days=3)).isoformat()}]
        s = wl.compute_stats(A, now=NOW)
        assert s["losses"] == 0 and s["resolved"] == 300 and s["sample_days"] < 1

    @pytest.mark.parametrize("name,clean", [("Alice", "Alice"), (A, None), (f"{A}-1789469513705", None), ("", None)])
    def test_clean_name(self, name, clean):
        assert wl.clean_name(name) == clean


class TestDiscovery:

    def test_candidates_filtered_ranked_and_stored(self, api):
        found = wl.discover_wallets(now=NOW)
        # B: posisi selesai cukup tetapi tidak ada aktivitas; C: tidak aktif 7 hari terakhir
        assert [c["address"] for c in found] == [A]
        c = found[0]
        assert c["rank"] == 1 and "peringkat #1 PnL cuaca bulan ini" in c["reason"] and "win rate 50%" in c["reason"]

    def test_tracked_and_skipped_are_excluded(self, api):
        wl.skip_wallet(A, now=NOW)
        assert wl.discover_wallets(now=NOW) == []


class TestTrackFollow:

    def test_track_follow_unfollow_untrack(self, api):
        w = wl.track_wallet(A, now=NOW)
        assert w["status"] == "tracking" and w["follow"] is False and w["stats"]["win_rate"] == 0.5
        assert wl.set_follow(A, True, now=NOW)["follow"] is True
        assert wl.set_follow(A, False, now=NOW)["follow"] is False
        assert wl.untrack_wallet(A) is True and wl.list_tracked() == []

    def test_follow_untracked_wallet_starts_tracking(self, api):
        w = wl.set_follow(A, True, now=NOW)
        assert w["follow"] is True and w["source"] == "discover"

    def test_skipped_wallet_hidden_from_list(self, api):
        wl.skip_wallet(B, now=NOW)
        assert wl.list_tracked() == [] and wl.list_tracked(include_skipped=True)[0]["status"] == "skipped"

    def test_resolve_by_number_and_name(self, api):
        wl.discover_wallets(now=NOW)
        assert wl.resolve_wallet("1") == A and wl.resolve_wallet("alice") == A
        with pytest.raises(wl.WalletError):
            wl.resolve_wallet("nobody")


class TestAlerts:

    def test_new_trades_alerted_once_grouped(self, api, sent):
        wl.set_follow(A, True, now=NOW - timedelta(minutes=30))
        api.activity[A] = [trade(TS - 60, usdc=10, size=20, tx="0x1"), trade(TS - 50, usdc=5, size=10, tx="0x2"),
                           trade(TS - 3600, tx="0xold")]  # sebelum mulai diikuti
        assert wl.poll_followed_wallets(now=NOW) == 1
        assert wl.poll_followed_wallets(now=NOW) == 0  # tidak dobel
        text = sent[0]
        assert "👛 Alice (0xaaaa…aaaa) bertransaksi:" in text
        assert "BUY Yes — Will the highest temperature in Hong Kong be 33°C?" in text
        assert "30.0 shares @ 50.0¢ ($15.00)" in text and "0xold" not in text
        assert f"https://polymarket.com/profile/{A}" in text

    def test_small_trades_ignored_but_marked(self, api, sent, monkeypatch):
        monkeypatch.setattr(settings, "WALLET_ALERT_MIN_USDC", 50.0)
        wl.set_follow(A, True, now=NOW - timedelta(minutes=5))
        api.activity[A] = [trade(TS - 60, usdc=10)]
        assert wl.poll_followed_wallets(now=NOW) == 0 and sent == []

    def test_weather_only_filter(self, api, sent, monkeypatch):
        monkeypatch.setattr(settings, "WALLET_ALERT_WEATHER_ONLY", True)
        wl.set_follow(A, True, now=NOW - timedelta(minutes=5))
        api.activity[A] = [trade(TS - 60, title="Will Bitcoin reach $200k?")]
        assert wl.poll_followed_wallets(now=NOW) == 0

    def test_failed_send_retried(self, api):
        wl.set_follow(A, True, now=NOW - timedelta(minutes=5))
        api.activity[A] = [trade(TS - 60)]
        with patch("app.paper_trading.telegram.send_telegram_message", return_value={"success": False}):
            assert wl.poll_followed_wallets(now=NOW) == 0
        with patch("app.paper_trading.telegram.send_telegram_message", return_value={"success": True}):
            assert wl.poll_followed_wallets(now=NOW) == 1

    def test_disabled(self, api, sent, monkeypatch):
        monkeypatch.setattr(settings, "WALLET_ALERTS", False)
        wl.set_follow(A, True, now=NOW - timedelta(minutes=5))
        api.activity[A] = [trade(TS - 60)]
        assert wl.poll_followed_wallets(now=NOW) == 0 and sent == []


class TestTelegram:

    def _msg(self, text):
        return handle_incoming_message(text, sender_chat_id="1", allowed_chat_id="1")

    def test_discover_has_follow_and_skip_buttons(self, api):
        reply = self._msg("/discover refresh")
        assert "Wallet menarik" in reply and "Kenapa:" in reply and "Alice" in reply
        buttons = [b for row in reply.reply_markup["inline_keyboard"] for b in row]
        assert {b["callback_data"] for b in buttons if not b["callback_data"].startswith("dc:")} == {f"wf:{A}", f"ws:{A}"}
        assert "dc:CRYPTO" in {b["callback_data"] for b in buttons}  # tombol pindah kategori

    def test_follow_by_number_then_wallets_list(self, api):
        self._msg("/discover refresh")
        assert "✅ Mengikuti Alice" in self._msg("/follow 1")
        listing = self._msg("/wallets")
        assert "🔔 Alice" in listing and "WR 50% (2/4)" in listing

    def test_wallet_detail_shows_activity(self, api):
        reply = self._msg(f"/wallet {A}")
        assert "Aktivitas terakhir" in reply and "TRADE BUY Yes" in reply and "belum dilacak" in reply
        assert reply.reply_markup["inline_keyboard"][0][0]["callback_data"] == f"wf:{A}"

    def test_invalid_query(self, api):
        assert "tidak ditemukan" in self._msg("/follow nobody")
        assert "Pakai:" in self._msg("/track")

    def test_callback_follow_and_unauthorized(self, api, sent):
        callback = {"id": "cb1", "data": f"wf:{A}", "message": {"chat": {"id": 1}}}
        with patch("app.paper_trading.telegram.answer_callback_query") as answer:
            reply = handle_callback_query(callback, token="T", allowed_chat_id="1")
            assert "Mengikuti" in reply and answer.called and wl.list_tracked()[0]["follow"] is True
            denied = handle_callback_query({**callback, "message": {"chat": {"id": 999}}}, token="T", allowed_chat_id="1")
        assert denied is None

    def test_help_lists_wallet_commands(self):
        text = self._msg("/help")
        assert "/discover" in text and "/wallets" in text and "/track" in text


class TestDashboardApi:

    def _client(self):
        from fastapi.testclient import TestClient
        from app.dashboard import app
        return TestClient(app)

    def test_track_follow_and_list(self, api):
        client = self._client()
        assert client.post("/api/wallets", json={"address": f"https://polymarket.com/profile/{A}"}).json()["address"] == A
        assert client.post(f"/api/wallets/{A}/follow").json()["follow"] is True
        data = client.get("/api/wallets").json()
        assert data["tracked"][0]["follow"] is True
        detail = client.get(f"/api/wallets/{A}").json()
        assert detail["stats"]["win_rate"] == 0.5 and detail["activity"][0]["side"] == "BUY"
        assert client.delete(f"/api/wallets/{A}").json()["removed"] is True

    def test_invalid_address_rejected(self, api):
        client = self._client()
        assert client.post("/api/wallets", json={"address": "0x" + "z" * 40}).status_code == 400
        assert client.post("/api/wallets/0x123/follow").status_code == 400

    def test_dashboard_has_wallet_section(self):
        assert 'id="walletTrackedBody"' in self._client().get("/").text


def test_avg_entry_margin_and_near_certain_reason(api):
    api.closed[A] = [dict(closed(1), avgPrice=0.97, totalBought=100)] * 10
    s = wl.compute_stats(A, now=NOW)
    assert s["avg_entry"] == 0.97 and s["margin"] == round(500 / 9000, 4)
    reason = wl.reason_for(s)
    assert "rata-rata beli 97¢ — pola 'hampir pasti'" in reason and "margin 5.6% dari volume" in reason


def test_score_balances_win_rate_and_margin():
    near_certain = {"wins": 99, "resolved": 100, "pnl": 100, "margin": 0.01}
    edge = {"wins": 60, "resolved": 100, "pnl": 100, "margin": 0.08}
    assert wl.score(edge) > wl.score(near_certain)


class TestCategories:

    def test_normalize_category_aliases(self):
        assert wl.normalize_category("kripto") == "CRYPTO" and wl.normalize_category("Sports") == "SPORTS"
        assert wl.normalize_category("olahraga") == "SPORTS" and wl.normalize_category("semua") == "OVERALL"
        assert wl.normalize_category("moon") is None and wl.normalize_category("") is None

    def test_discovery_is_per_category(self, api):
        wl.discover_wallets(now=NOW, category="CRYPTO")
        params = [p for path, p in api.calls if path == "/v1/leaderboard" and not p.get("user")]
        assert params[-1]["category"] == "CRYPTO"
        assert [c["address"] for c in wl.list_candidates("CRYPTO")] == [A]
        assert wl.list_candidates("CRYPTO")[0]["category"] == "CRYPTO"
        assert "PnL kripto bulan ini" in wl.list_candidates("CRYPTO")[0]["reason"]
        assert wl.list_candidates("WEATHER") == []  # kategori lain tidak tercampur
        assert wl.candidates_age(category="WEATHER") is None and wl.candidates_age(category="CRYPTO") is not None
        wl.discover_wallets(now=NOW, category="WEATHER")
        assert len(wl.list_candidates("CRYPTO")) == 1 and len(wl.list_candidates("WEATHER")) == 1

    def test_discover_command_and_category_button(self, api):
        reply = handle_incoming_message("/discover kripto", sender_chat_id="1", allowed_chat_id="1")
        assert "Wallet menarik · market kripto" in reply
        assert "/discover kripto" not in handle_incoming_message("/discover moon", sender_chat_id="1", allowed_chat_id="1")
        assert "tidak dikenal" in handle_incoming_message("/discover moon", sender_chat_id="1", allowed_chat_id="1")
        assert "✅ Mengikuti Alice" in handle_incoming_message("/follow 1", sender_chat_id="1", allowed_chat_id="1")
        with patch("app.paper_trading.telegram.answer_callback_query", return_value={"success": True}), \
             patch("app.paper_trading.telegram_bot.send_telegram_message", return_value={"success": True}):
            reply = handle_callback_query({"id": "q", "data": "dc:SPORTS", "message": {"chat": {"id": 1}}},
                                          token="t", allowed_chat_id="1")
        assert "market olahraga" in reply

    def test_dashboard_api_category(self, api):
        from fastapi.testclient import TestClient
        from app.dashboard import app
        client = TestClient(app)
        assert client.post("/api/wallets/discover", params={"category": "SPORTS"}).status_code == 200
        data = client.get("/api/wallets", params={"category": "sports"}).json()
        assert data["category"] == "SPORTS" and data["candidates"][0]["address"] == A
        assert client.post("/api/wallets/discover", params={"category": "moon"}).status_code == 400
        assert 'id="walletCategory"' in client.get("/").text


class TestPortfolioOverlap:

    def _me(self, monkeypatch, api, outcome="Yes"):
        me = "0x" + "e" * 40
        monkeypatch.setattr(settings, "POLYMARKET_WALLET_ADDRESS", me)
        api.positions[me] = [{"conditionId": "0xc1", "outcome": outcome, "size": 12, "avgPrice": 0.45, "curPrice": 0.6,
                              "title": "Will the highest temperature in Hong Kong be 33°C?", "eventSlug": "hk"}]
        return me

    def test_trade_alert_marks_same_market_as_portfolio(self, api, sent, monkeypatch):
        self._me(monkeypatch, api)
        wl.set_follow(A, True, now=NOW - timedelta(hours=1))
        api.activity[A] = [trade(TS - 60, side="SELL")]
        assert wl.poll_followed_wallets(now=NOW) == 1
        text = sent[-1]
        assert "📌 market yang sama dengan porto Anda" in text
        assert "Porto Anda juga di market ini: Yes 12.0 sh @ 45¢ — ⚠️ wallet ini MENJUAL sisi yang Anda pegang" in text

    def test_overlap_alert_once_with_direction(self, api, sent, monkeypatch):
        self._me(monkeypatch, api, outcome="No")
        wl.set_follow(A, True, now=NOW)
        api.positions[A] = [{"conditionId": "0xc1", "outcome": "Yes", "size": 300, "avgPrice": 0.4, "title": "HK 33",
                             "eventSlug": "hk"}, {"conditionId": "0xother", "outcome": "Yes", "size": 5, "avgPrice": 0.5}]
        sent.clear()
        assert wl.check_portfolio_overlap(NOW) == 1
        assert wl.check_portfolio_overlap(NOW) == 0  # sekali per wallet & market
        text = sent[0]
        assert text.startswith("🤝 Porto Anda sama dengan Alice")
        assert "Anda: No 12.0 sh @ 45¢" in text and "Wallet: Yes 300.0 sh @ 40¢" in text and "berlawanan" in text

    def test_no_overlap_check_without_own_wallet(self, api, sent, monkeypatch):
        monkeypatch.setattr(settings, "POLYMARKET_WALLET_ADDRESS", None)
        assert wl.check_portfolio_overlap(NOW) == 0


class TestAntiSpam:

    def test_same_market_alerted_once_per_day_and_cooldown(self, api, sent, monkeypatch):
        monkeypatch.setattr(settings, "POLYMARKET_WALLET_ADDRESS", None)
        wl.set_follow(A, True, now=NOW - timedelta(hours=3))
        api.activity[A] = [trade(TS - 7000, tx="0x1", cond="0xm1")]
        assert wl.poll_followed_wallets(now=NOW - timedelta(minutes=110)) == 1
        # wallet yang sama menambah di market yang sama: tidak dikirim lagi
        api.activity[A] = [trade(TS - 7000, tx="0x1", cond="0xm1"), trade(TS - 4000, tx="0x2", cond="0xm1")]
        assert wl.poll_followed_wallets(now=NOW - timedelta(minutes=60)) == 0
        # market baru tapi masih dalam jeda 60 menit sejak alert terakhir: dilewati & dihitung
        api.activity[A].append(trade(TS - 3500, tx="0x3", cond="0xm2", title="Market dua"))
        assert wl.poll_followed_wallets(now=NOW - timedelta(minutes=55)) == 0
        # setelah jeda: market baru dikirim, dengan catatan market yang dilewati
        api.activity[A].append(trade(TS - 60, tx="0x4", cond="0xm3", title="Market tiga"))
        assert wl.poll_followed_wallets(now=NOW) == 1
        assert len(sent) == 2
        assert "Market tiga" in sent[1] and "Market dua" not in sent[1]
        assert "+1 market lain sejak alert terakhir tidak dikirim" in sent[1]

    def test_portfolio_market_bypasses_cooldown_but_still_once(self, api, sent, monkeypatch):
        me = "0x" + "e" * 40
        monkeypatch.setattr(settings, "POLYMARKET_WALLET_ADDRESS", me)
        api.positions[me] = [{"conditionId": "0xmine", "outcome": "Yes", "size": 5, "avgPrice": 0.5, "curPrice": 0.5,
                              "title": "Mine"}]
        wl.set_follow(A, True, now=NOW - timedelta(hours=3))
        api.activity[A] = [trade(TS - 600, tx="0x1", cond="0xm1")]
        assert wl.poll_followed_wallets(now=NOW - timedelta(minutes=5)) == 1
        api.activity[A].append(trade(TS - 60, tx="0x2", cond="0xmine", side="SELL", title="Mine"))
        assert wl.poll_followed_wallets(now=NOW) == 1  # jeda diabaikan: market di porto sendiri
        assert "MENJUAL sisi yang Anda pegang" in sent[-1]
        api.activity[A].append(trade(TS - 30, tx="0x3", cond="0xmine", side="SELL", title="Mine"))
        assert wl.poll_followed_wallets(now=NOW + timedelta(minutes=1)) == 0  # tetap sekali per market


def test_duplicate_activity_rows_do_not_cause_repeated_alerts(api, sent, monkeypatch):
    """Satu transaksi muncul di beberapa baris /activity: alert sekali, log tersimpan, tidak terkirim ulang."""
    monkeypatch.setattr(settings, "POLYMARKET_WALLET_ADDRESS", None)
    wl.set_follow(A, True, now=NOW - timedelta(hours=1))
    dup = trade(TS - 60, tx="0xdup", asset="as9", cond="0xmd")
    api.activity[A] = [dup, dict(dup), dict(dup, size=10.0, usdcSize=5.0)]
    assert wl.poll_followed_wallets(now=NOW) == 1
    assert wl.poll_followed_wallets(now=NOW + timedelta(minutes=1)) == 0
    assert wl.poll_followed_wallets(now=NOW + timedelta(minutes=2)) == 0
    assert len(sent) == 1
