"""Test bot Hong Kong: peluang bracket max/min, estimasi, keputusan, eksekusi paper, dan AI (Gemini dipalsukan)."""
import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.core.database import get_db_session
from app.paper_trading import hk_ai, hk_bot
from app.paper_trading.models import HkForecastView, StationReading
from app.paper_trading.telegram_bot import handle_incoming_message

HKT = ZoneInfo("Asia/Hong_Kong")


def hkt(hour, minute=0, day=9):
    return datetime(2026, 10, day, hour, minute, tzinfo=HKT)


@pytest.fixture(autouse=True)
def fresh_sigma(monkeypatch):
    monkeypatch.setattr(hk_bot, "_sigma_cache", {"at": 0.0, "values": None})
    monkeypatch.setattr(hk_bot, "lead_errors", lambda days=30, now=None: {})
    monkeypatch.setattr(hk_ai, "_summary_cache", {"at": 0.0, "report": None})


def market(bracket, ask, bid, mid="0x"):
    return {"bracket": bracket, "yes_token_id": f"tok-{bracket}", "market_id": f"{mid}{bracket}", "price_yes": ask,
            "ask": ask, "bid": bid}


def status(now, temp=30.2, mx=30.6, mn=25.4, official=None):
    return {"observed_at": now.astimezone(HKT), "temp": temp, "max": mx, "min": mn, "rate": -0.2, "estimate": mx,
            "min_estimate": mn, "official": official or {"text": "Sunny periods. Hot.", "max_hint": None, "min_hint": None},
            "market": [market("29°C or below", 0.02, 0.01), market("30°C", 0.40, 0.38), market("31°C", 0.45, 0.43),
                       market("32°C or higher", 0.12, 0.10)],
            "min_market": [market("24°C or below", 0.10, 0.08, "0xn"), market("25°C", 0.80, 0.78, "0xn"),
                           market("26°C or higher", 0.05, 0.03, "0xn")]}


def test_final_distribution_respects_observed_extremes():
    # max terukur 30.4; puncak sisa hari ~ N(31, 0.5): bracket 29°C mustahil, 30°C = P(X < 31) = 50%
    assert hk_bot.bracket_prob((29, 29), "highest", 30.4, 31.0, 0.5) == 0.0
    assert hk_bot.bracket_prob((30, 30), "highest", 30.4, 31.0, 0.5) == pytest.approx(0.5)
    total = sum(hk_bot.bracket_prob(b, "highest", 30.4, 31.0, 0.5)
                for b in [(-math.inf, 29), (30, 30), (31, 31), (32, math.inf)])
    assert total == pytest.approx(1.0)
    # min terukur 25.4: bracket 26°C ke atas mustahil, 25°C = P(lembah ≥ 25)
    assert hk_bot.bracket_prob((26, math.inf), "lowest", 25.4, 25.0, 0.5) == 0.0
    assert hk_bot.bracket_prob((25, 25), "lowest", 25.4, 25.0, 0.5) == pytest.approx(0.5)


def test_estimate_uses_projection_peak_official_hint_and_final_rule():
    now = hkt(11).astimezone(timezone.utc)
    proj = [(hkt(h), v) for h, v in ((12, 30.8), (13, 31.4), (14, 31.2), (20, 27.0), (23, 25.9))]
    st = status(now, official={"text": "Hot", "max_hint": 32.0, "min_hint": 25.0})
    est = hk_bot.estimate("highest", now, st, proj)
    assert est["projected"] == 31.4 and est["mu"] == pytest.approx(31.7)  # rata-rata dengan resmi 32 (bobot 0.5)
    assert est["lead"] == pytest.approx(2.0) and est["sigma"] == pytest.approx(0.6)
    low = hk_bot.estimate("lowest", now, st, proj)
    assert low["projected"] == 25.9 and low["mu"] == pytest.approx(25.45)
    late = hkt(17, 30).astimezone(timezone.utc)
    est = hk_bot.estimate("highest", late, status(late, temp=29.0), [(hkt(18), 28.8)])
    assert est["final"] and est["sigma"] == hk_bot.SIGMA_FINAL


def test_sigma_uses_measured_errors_with_floor(monkeypatch):
    monkeypatch.setattr(hk_bot, "lead_errors", lambda days=30, now=None: {1: (0.1, 200), 3: (1.1, 200), 4: (2.0, 5)})
    assert hk_bot.sigma_for(1) == pytest.approx(0.3)      # 0.1 terukur → batas bawah 0.3
    assert hk_bot.sigma_for(3) == pytest.approx(1.1)      # sampel cukup: pakai terukur
    assert hk_bot.sigma_for(4) == pytest.approx(0.8)      # sampel sedikit: default
    assert hk_bot.sigma_for(10) == pytest.approx(1.4)     # di luar 6 jam: + 0.1/jam


@pytest.fixture
def books(monkeypatch):
    from app.paper_trading import autotrader as at
    asks = {}
    monkeypatch.setattr(at, "_book_side", lambda token, usd: {"price": asks.get(token, 0.40), "fee": 0.01, "spread": 0.02,
                                                              "shares": 10, "slippage": 0.0})
    return asks


def test_evaluate_picks_best_edge_and_applies_rules(books, monkeypatch):
    from app.paper_trading import autotrader as at
    monkeypatch.setattr(settings, "AUTOTRADE_HK_MODEL_WEIGHT", 1.0)
    now = hkt(16, 30).astimezone(timezone.utc)
    st = status(now, temp=29.8, mx=30.6)
    analysis = hk_bot.analyze(now, status=st, projection=[(hkt(17), 29.6), (hkt(18), 29.0)])
    books["tok-30°C"] = 0.40
    d = hk_bot.evaluate("hk_max", now, analysis)
    assert d["features"]["bracket"] == "30°C" and d["skip_reason"] is None
    assert d["prob"] > 0.9 and d["edge"] == pytest.approx(d["prob"] - 0.41)
    assert d["key"] == "hk_max|2026-10-09" and d["title"].startswith("🇭🇰 Hong Kong max")
    early = hkt(8).astimezone(timezone.utc)
    early_analysis = hk_bot.analyze(early, status=status(early), projection=[(hkt(13), 31.0)])
    assert "sebelum jam 9" in hk_bot.evaluate("hk_max", early, early_analysis)["skip_reason"]
    monkeypatch.setattr(at, "already_decided", lambda key: True)
    assert hk_bot.evaluate("hk_max", now, analysis)["skip_reason"] == "sudah trade hari ini"


def test_market_weight_shrinks_model(books):
    now = hkt(16, 30).astimezone(timezone.utc)
    analysis = hk_bot.analyze(now, status=status(now, temp=29.8), projection=[(hkt(17), 29.6)])
    row = next(r for r in analysis["max"]["brackets"] if r["bracket"] == "30°C")
    assert row["prob"] == pytest.approx(row["market_prob"] + 0.6 * (row["model"] - row["market_prob"]), abs=1e-3)


def test_hk_tick_executes_active_and_shadows_inactive(books, monkeypatch):
    from app.paper_trading import autotrader as at
    now = hkt(16, 30).astimezone(timezone.utc)
    analysis = hk_bot.analyze(now, status=status(now, temp=29.8), projection=[(hkt(17), 29.6)])
    monkeypatch.setattr(hk_bot, "analyze", lambda now=None, status=None, projection=None: analysis)
    executed, logged = [], []
    monkeypatch.setattr(at, "execute", lambda d, now=None: executed.append(d["strategy"]) or {"shares": 1})
    monkeypatch.setattr(at, "log_signal", lambda d, key, skip, now: logged.append((d["strategy"], skip)) or True)
    made = hk_bot.hk_tick(now, strategies=["hk_max"])
    assert executed == ["hk_max"] and [d["strategy"] for d in made] == ["hk_max"]
    assert ("hk_min", "strategi nonaktif") in logged or any(s == "hk_min" and r for s, r in logged)


def test_strategy_registered_in_autotrader():
    from app.paper_trading import autotrader as at
    assert at.STRATEGY_VERSIONS["hk_max"] == "auto_hk_max_v1" and "hk_min" in at.get_config()["STRATEGIES"]["options"]
    assert at.strategy_header("hk_min") == "🇭🇰 HONG KONG · MIN"
    assert at._versions_for("hk_all") == ["auto_hk_max_v1", "auto_hk_min_v1"]
    assert at.cfg("HK_MIN_EDGE") == pytest.approx(0.08)


# --- AI ------------------------------------------------------------------------------------

@pytest.fixture
def ai(monkeypatch, books):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "HK_AI_ENABLED", True)
    now = hkt(15).astimezone(timezone.utc)
    st = status(now)
    monkeypatch.setattr("app.paper_trading.hko_alerts.hko_status", lambda now=None, db=None: st)
    monkeypatch.setattr(hk_bot, "_projection", lambda now: [(hkt(16), 30.4), (hkt(22), 26.0)])
    monkeypatch.setattr(hk_bot, "positions_today", lambda now=None: [])
    calls = []

    def fake_gemini(prompt, system, json_mode=False, timeout=60):
        calls.append({"prompt": prompt, "system": system, "json": json_mode})
        if json_mode:
            return json.dumps({"max": {"perkiraan": 30.9, "peluang": {"30°C": 6, "31°C": 3, "32°C or higher": 1, "lain": 5}},
                               "min": {"perkiraan": 25.2, "peluang": {"25°C": 0.9, "24°C or below": 0.1}},
                               "ringkasan": "Cerah, max sudah 30.6°C.", "alasan": ["puncak lewat"], "risiko": ["hujan sore"]})
        return "Peluang max ≥ 31°C sekitar 30%."
    monkeypatch.setattr(hk_ai, "gemini", fake_gemini)
    return {"now": now, "calls": calls}


def test_ai_view_normalizes_to_market_brackets(ai):
    view = hk_ai.ai_view(ai["now"])
    assert view["max"]["probs"] == {"29°C or below": 0.0, "30°C": 0.6, "31°C": 0.3, "32°C or higher": 0.1}
    assert view["min"]["point"] == 25.2 and view["ringkasan"].startswith("Cerah")
    call = ai["calls"][0]
    assert call["json"] and "Karakteristik Suhu di Hong Kong" in call["system"] and '"max_hari_ini": 30.6' in call["prompt"]


def test_summary_records_views_and_scores_after_day_ends(ai):
    report = hk_ai.build_summary(ai["now"])
    assert "RINGKASAN HONG KONG" in report["text"] and "AI (Gemini, bayangan)" in report["text"]
    assert "Model bot" in report["text"] and "Cerah, max sudah 30.6°C." in report["text"]
    db = get_db_session()
    try:
        assert {(v.kind, v.source) for v in db.query(HkForecastView).all()} == {
            (k, s) for k in ("max", "min") for s in ("ai", "model", "market")}
        # hari itu selesai: max 30.9, min 25.1 dari bacaan 10 menit
        start = hkt(0)
        for i in range(144):
            t = 25.1 + 5.8 * math.sin(math.pi * i / 143)
            db.add(StationReading(station="HKO", observed_at=(start + timedelta(minutes=10 * i)).astimezone(timezone.utc),
                                  temp=Decimal(str(round(t, 1)))))
        db.commit()
    finally:
        db.close()
    score = hk_ai.scorecard(now=hkt(10, day=10).astimezone(timezone.utc))
    assert set(score) == {"ai", "model", "market"} and score["ai"]["n"] == 2 and score["ai"]["days"] == 1
    # max 30.9 → 30°C: (0.6−1)² + 0.3² + 0.1² = 0.26; min 25.1 → 25°C: 0.1² + 0.1² = 0.02
    assert score["ai"]["brier"] == pytest.approx((0.26 + 0.02) / 2, abs=1e-3)


def test_summary_without_ai_still_sends_model_part(ai, monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setattr(hk_ai, "gemini", lambda *a, **k: (_ for _ in ()).throw(hk_ai.GeminiError("GEMINI_API_KEY belum diisi di .env")))
    text = hk_ai.build_summary(ai["now"], use_cache=False)["text"]
    assert "AI tidak tersedia" in text and "Model bot" in text


def test_scheduled_report_once_per_slot(ai, monkeypatch):
    sent = []
    monkeypatch.setattr("app.paper_trading.telegram.send_telegram_message",
                        lambda text, parse_mode=None, **kw: sent.append(text) or {"success": True})
    jkt = ZoneInfo("Asia/Jakarta")
    at = datetime(2026, 10, 9, 15, 5, tzinfo=jkt).astimezone(timezone.utc)
    assert hk_ai.maybe_send_scheduled(at) is True
    assert hk_ai.maybe_send_scheduled(at + timedelta(minutes=10)) is False   # slot yang sama
    assert hk_ai.maybe_send_scheduled(at + timedelta(minutes=60)) is False   # 16:05 bukan jam laporan
    assert hk_ai.maybe_send_scheduled(at + timedelta(hours=3)) is True       # 18:05
    assert len(sent) == 2 and hk_ai.report_hours() == [0, 3, 6, 9, 12, 15, 18, 21]


def test_ask_and_rate_limit(ai, monkeypatch):
    assert hk_ai.ask("peluang max 31?", ai["now"]) == "Peluang max ≥ 31°C sekitar 30%."
    assert "Pertanyaan pengguna: peluang max 31?" in ai["calls"][-1]["prompt"]
    monkeypatch.setattr(settings, "HK_AI_ASK_PER_HOUR", 1)
    assert "Batas 1 pertanyaan" in hk_ai.ask("lagi?", ai["now"])
    assert "Tulis pertanyaannya" in hk_ai.ask("  ", ai["now"])


def test_gemini_request_keeps_key_out_of_url(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "secret-123")
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "berpikir", "thought": True},
                                                                      {"text": "halo"}]}}]}).encode()

    def fake_urlopen(req, timeout=60):
        seen["url"], seen["headers"], seen["body"] = req.full_url, dict(req.header_items()), json.loads(req.data)
        return Resp()
    monkeypatch.setattr(hk_ai.urllib.request, "urlopen", fake_urlopen)
    assert hk_ai.gemini("p", "s", json_mode=True) == "halo"
    assert "secret-123" not in seen["url"] and seen["headers"]["X-goog-api-key"] == "secret-123"
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"


def test_telegram_commands_and_api(ai):
    reply = handle_incoming_message("/rangkum", "1")
    assert "RINGKASAN HONG KONG" in reply
    assert handle_incoming_message("/tanya peluang max 31?", "1").startswith("🤖 Peluang max")
    assert "/tanya" in handle_incoming_message("/help", "1")
    from fastapi.testclient import TestClient
    from app.dashboard import app
    client = TestClient(app)
    data = client.get("/api/hk/bot").json()
    assert data["analysis"]["max"]["observed"] == 30.6 and data["analysis"]["max"]["brackets"][0]["lo"] is None  # -inf → null
    assert client.post("/api/hk/ask", json={"question": "peluang?"}).json()["answer"].startswith("Peluang")
    assert client.post("/api/hk/ask", json={"question": " "}).status_code == 400
    assert "RINGKASAN" in client.post("/api/hk/summary").json()["text"]
    assert 'id="chatInput"' in client.get("/hk").text
