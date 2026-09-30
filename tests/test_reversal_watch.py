"""
Test "waspada berbalik": bracket favorit ≥ 90¢ dengan indikasi hasil bisa berubah.
"""
import math
from datetime import date, datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.paper_trading import reversal_watch as rw

HKT = ZoneInfo("Asia/Hong_Kong")
NOW = datetime(2026, 9, 30, 15, 20, tzinfo=HKT).astimezone(timezone.utc)


def br(label, price, token=None):
    return {"label": label, "range": rw._bracket(label), "price": price, "token": token or f"t{label}", "market_id": f"0x{label}"}


def hk_event(fav_price=0.96):
    return {"city": "Hong Kong", "kind": "highest", "tz": HKT, "date": date(2026, 9, 30), "station": "HKO",
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


class TestCheck:

    def _run(self, monkeypatch, fav_price=0.96, observation=None, momentum=None):
        sent = []
        monkeypatch.setattr(rw, "today_events", lambda now, cities: [hk_event(fav_price)])
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
