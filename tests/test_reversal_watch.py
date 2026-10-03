"""
Test "waspada berbalik": bracket favorit ≥ 90¢ dengan indikasi hasil bisa berubah.
"""
import math
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from decimal import Decimal

from app.core.config import settings
from app.core.database import get_db_session
from app.paper_trading import reversal_watch as rw
from app.paper_trading.models import MarketLatest, MarketResolution, ReversalWatch

HKT = ZoneInfo("Asia/Hong_Kong")
NOW = datetime(2026, 9, 30, 15, 20, tzinfo=HKT).astimezone(timezone.utc)


def br(label, price, token=None):
    return {"label": label, "range": rw._bracket(label), "price": price, "token": token or f"t{label}", "market_id": f"0x{label}"}


def hk_event(fav_price=0.96, volume=50000.0):
    return {"city": "Hong Kong", "kind": "highest", "tz": HKT, "date": date(2026, 9, 30), "station": "HKO", "volume": volume,
            "brackets": [br("32°C", 0.01), br("33°C", fav_price), br("34°C", 0.03), br("35°C or higher", 0.004)]}


def obs(extreme, current, rate, final=False, decimal=True, hint=None):
    return {"extreme": extreme, "current": current, "rate": rate, "final": final, "decimal": decimal, "unit": "C",
            "official_hint": hint, "final_note": "sebelum 17:00 HKT"}


class TestIndicators:

    def test_case_30_sep_near_boundary_and_momentum(self):
        ev = hk_event()
        fav, nb = ev["brackets"][1], ev["brackets"][2]
        found = rw.indicators(ev, fav, nb, obs(33.8, 33.8, 0.6), {"before": 0.028, "now": 0.229, "change": 0.201})
        assert any("max 33.8°C tinggal 0.2° dari batas 33°C, suhu naik +0.6°/jam" in f for f in found)
        assert any("bracket 34°C naik 3¢ → 23¢" in f for f in found)

    def test_quiet_market_no_alert(self):
        ev = hk_event()
        assert rw.indicators(ev, ev["brackets"][1], ev["brackets"][2], obs(33.3, 32.6, -0.4),
                             {"before": 0.03, "now": 0.02, "change": -0.01}) == []

    def test_final_suppresses_boundary_indicator(self):
        ev = hk_event()
        assert rw.indicators(ev, ev["brackets"][1], ev["brackets"][2], obs(33.8, 33.8, 0.2, final=True), None) == []

    def test_already_exceeded_and_official_forecast(self):
        ev = hk_event()
        found = rw.indicators(ev, ev["brackets"][1], ev["brackets"][2], obs(34.1, 34.0, 0.3, hint=35.0), None)
        assert any("sudah 34.1°C, di atas bracket 33°C" in f for f in found)
        assert any("prakiraan resmi HKO ±35°C" in f for f in found)

    def test_metar_integer_reading_at_top_of_bracket(self):
        ev = {"city": "Tokyo", "kind": "highest", "tz": ZoneInfo("Asia/Tokyo"), "date": date(2026, 9, 30),
              "brackets": [br("25°C", 0.93), br("26°C", 0.05)]}
        found = rw.indicators(ev, ev["brackets"][0], ev["brackets"][1], obs(25, 25, 0.5, decimal=False), None)
        assert found and "tinggal 0.5°" in found[0]

    def test_lowest_falling_toward_lower_bound(self):
        ev = {"city": "London", "kind": "lowest", "tz": ZoneInfo("Europe/London"), "date": date(2026, 9, 30),
              "brackets": [br("12°C", 0.92), br("11°C", 0.05)]}
        nb = rw._neighbor(ev, ev["brackets"][0])
        assert nb["label"] == "11°C"
        found = rw.indicators(ev, ev["brackets"][0], nb, obs(12, 11.9, -0.8, decimal=False), None)
        assert found and "turun -0.8°/jam" in found[0]

    def test_neighbor_for_highest(self):
        ev = hk_event()
        assert rw._neighbor(ev, ev["brackets"][1])["label"] == "34°C"
        assert rw._neighbor(ev, ev["brackets"][3]) is None  # "or higher" tidak punya bracket di atas


def clear_watches():
    db = get_db_session()
    db.query(ReversalWatch).delete()
    db.query(MarketLatest).filter(MarketLatest.market_id.like("0x%°%")).delete(synchronize_session=False)
    db.query(MarketResolution).filter(MarketResolution.market_id.like("0x%°%")).delete(synchronize_session=False)
    db.commit()
    db.close()


def set_prices(prices):
    db = get_db_session()
    for label, price in prices.items():
        mid = f"0x{label}"
        row = db.get(MarketLatest, mid) or MarketLatest(market_id=mid, market_name=label, status="open",
                                                        is_resolved=False, timestamp=NOW)
        row.price_yes = Decimal(str(price))
        db.merge(row)
    db.commit()
    db.close()


class TestCheck:

    def _run(self, monkeypatch, fav_price=0.96, observation=None, momentum=None, volume=50000.0, liquid=True):
        sent = []
        clear_watches()
        monkeypatch.setattr(rw, "today_events", lambda now, cities: [hk_event(fav_price, volume)])
        monkeypatch.setattr(rw, "liquidity", lambda fav: {"spread": 0.01, "bid": 0.95, "bid_depth_usd": 500} if liquid else None)
        monkeypatch.setattr(rw, "observation", lambda event, now: observation)
        monkeypatch.setattr(rw, "price_momentum", lambda token, now: momentum)
        monkeypatch.setattr("app.paper_service.get_city_volume_summary", lambda limit=7, now=None: [])
        with patch("app.paper_trading.telegram.send_telegram_message",
                   side_effect=lambda text, **kw: sent.append(text) or {"success": True}):
            rw.check_reversals(NOW)
            rw.check_reversals(NOW)  # siklus berikutnya: tidak dobel
        return sent

    def test_alert_sent_once(self, monkeypatch):
        sent = self._run(monkeypatch, observation=obs(33.8, 33.8, 0.6),
                         momentum={"before": 0.028, "now": 0.229, "change": 0.201})
        assert len(sent) == 1
        text = sent[0]
        assert text.startswith("🔄 WASPADA BERBALIK · #HongKong max · bracket 33°C di 96¢")
        assert "Bracket sebelah: 34°C" in text and "belum final" in text.lower()

    def test_no_alert_below_min_price(self, monkeypatch):
        assert self._run(monkeypatch, fav_price=0.80, observation=obs(33.8, 33.8, 0.6)) == []

    def test_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "REVERSAL_ALERTS", False)
        assert rw.check_reversals(NOW) == []

    def test_momentum_from_price_history(self, monkeypatch):
        ts = int(NOW.timestamp())
        history = '{"history": [{"t": %d, "p": 0.03}, {"t": %d, "p": 0.04}, {"t": %d, "p": 0.22}]}' % (
            ts - 40 * 60, ts - 30 * 60, ts - 60)
        monkeypatch.setattr("app.paper_trading.live_market_data._http", lambda url, **kw: history)
        m = rw.price_momentum("tok", NOW)
        assert m["before"] == 0.04 and m["now"] == 0.22 and m["change"] == pytest.approx(0.18)


class TestFilterAndFollowUp:

    def test_small_volume_or_illiquid_market_is_ignored(self, monkeypatch):
        momentum = {"before": 0.028, "now": 0.229, "change": 0.201}
        assert TestCheck()._run(monkeypatch, observation=obs(33.8, 33.8, 0.6), momentum=momentum, volume=5000) == []
        assert TestCheck()._run(monkeypatch, observation=obs(33.8, 33.8, 0.6), momentum=momentum, liquid=False) == []
        assert rw.reversal_history()["summary"]["tracked"] == 0

    def test_liquidity_check_uses_spread_and_bid_depth(self, monkeypatch):
        books = {"t33°C": {"ask": 0.97, "bid": 0.96, "bid_depth_usd": 450.0}}
        monkeypatch.setattr("app.paper_trading.live_market_data.fetch_order_books", lambda tokens: books)
        fav = br("33°C", 0.965)
        assert rw.liquidity(fav)["bid_depth_usd"] == 450.0
        books["t33°C"] = {"ask": 0.99, "bid": 0.90, "bid_depth_usd": 450.0}  # spread 9¢
        assert rw.liquidity(fav) is None
        books["t33°C"] = {"ask": 0.97, "bid": 0.96, "bid_depth_usd": 20.0}   # bid tipis
        assert rw.liquidity(fav) is None

    def test_confirmed_reversal_after_warning(self, monkeypatch):
        sent = TestCheck()._run(monkeypatch, observation=obs(33.8, 33.8, 0.6),
                                momentum={"before": 0.028, "now": 0.229, "change": 0.201})
        assert len(sent) == 1 and "WASPADA BERBALIK" in sent[0]
        set_prices({"32°C": 0.01, "33°C": 0.12, "34°C": 0.86, "35°C or higher": 0.01})
        with patch("app.paper_trading.telegram.send_telegram_message",
                   side_effect=lambda text, **kw: sent.append(text) or {"success": True}):
            rw.follow_up(NOW)
            rw.follow_up(NOW)  # sekali saja
        assert len(sent) == 2
        flip = sent[1]
        assert flip.startswith("🔁 BENAR BERBALIK · #HongKong max 2026-09-30")
        assert "33°C sempat 96¢ → sekarang 12¢" in flip and "Pemimpin baru: 34°C di 86¢" in flip
        assert "✅ Warning waspada berbalik sudah dikirim" in flip
        item = rw.reversal_history()["items"][0]
        assert item["verdict"] == "warning tepat" and item["flip"] == "33°C 12¢ → 34°C 86¢"

    def test_reversal_without_warning_found_at_resolution(self, monkeypatch):
        sent = TestCheck()._run(monkeypatch, observation=obs(33.0, 32.5, -0.2))  # tenang: tanpa warning
        assert sent == []
        set_prices({"32°C": 0.01, "33°C": 0.97, "34°C": 0.02, "35°C or higher": 0.0})
        db = get_db_session()
        db.add_all([MarketResolution(market_id="0x33°C", winning_outcome="NO", resolved_at=NOW),
                    MarketResolution(market_id="0x34°C", winning_outcome="YES", resolved_at=NOW)])
        db.commit()
        db.close()
        monkeypatch.setattr("app.market_collector.collector.sync_markets_by_condition_ids", lambda ids: None)
        later = NOW + timedelta(days=1)
        with patch("app.paper_trading.telegram.send_telegram_message",
                   side_effect=lambda text, **kw: sent.append(text) or {"success": True}):
            rw.follow_up(later)
        assert len(sent) == 1 and "saat resolve 0¢" in sent[0] and "Pemimpin baru: 34°C" in sent[0]
        assert "Tanpa warning sebelumnya" in sent[0]
        data = rw.reversal_history()
        assert data["items"][0]["verdict"] == "terlewat" and data["items"][0]["outcome"] == "reversed"
        assert data["summary"]["reversed"] == 1 and data["summary"]["reversed_warned"] == 0
        assert "terlewat" in rw.format_reversal_history()

    def test_false_alarm_when_favourite_holds(self, monkeypatch):
        TestCheck()._run(monkeypatch, observation=obs(33.8, 33.8, 0.6),
                         momentum={"before": 0.028, "now": 0.229, "change": 0.201})
        set_prices({"33°C": 0.99, "34°C": 0.01})
        db = get_db_session()
        db.merge(MarketResolution(market_id="0x33°C", winning_outcome="YES", resolved_at=NOW))
        db.commit()
        db.close()
        monkeypatch.setattr("app.market_collector.collector.sync_markets_by_condition_ids", lambda ids: None)
        with patch("app.paper_trading.telegram.send_telegram_message", return_value={"success": True}) as send:
            rw.follow_up(NOW + timedelta(days=1))
        assert not send.called
        item = rw.reversal_history()["items"][0]
        assert item["outcome"] == "held" and item["verdict"] == "alarm palsu"
