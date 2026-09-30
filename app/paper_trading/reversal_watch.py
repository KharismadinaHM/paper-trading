"""
"Waspada berbalik": alert saat bracket favorit market suhu hari ini sudah ≥ REVERSAL_MIN_PRICE (90¢)
tetapi ada indikasi hasil akhirnya bisa berubah.

Indikator (dicek tiap siklus collector untuk Hong Kong + kota top volume):
1. dekat batas: suhu ekstrem terukur sudah dekat titik pindah bracket (HKO 0.1°C: ≤ 0.3°C;
   METAR bulat: bacaan sudah di angka teratas bracket) DAN suhu masih naik / bertahan, sebelum final;
2. momentum pasar: harga bracket sebelah naik ≥ REVERSAL_MOMENTUM (8¢) dalam 30 menit;
3. sudah lewat: data stasiun sudah melewati bracket favorit, tetapi harganya masih ≥ 90¢;
4. (HK) prakiraan resmi HKO menyebut suhu di atas bracket favorit.
Alert bila indikator 3 atau 4 aktif, atau indikator 1 dan/atau 2 aktif. Sekali per bracket per hari.

Kasus acuan (30 Sep 2026, Hong Kong): 33°C 98.5¢ pukul 14:40; 15:20 bracket 34°C melonjak 2.8¢ → 22.9¢;
HKO mencatat 34.2°C sekitar 15:50.
"""
import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.models import StationAlert

logger = get_logger("reversal_watch")


def _bracket(label: str):
    from app.paper_trading.autotrader import bracket_range
    return bracket_range(label or "")


def today_events(now: datetime, cities: List[str]) -> List[Dict[str, Any]]:
    """Market suhu hari ini (tanggal lokal kota) per kota & jenis: bracket + token + harga mid."""
    from app.paper_service import get_market_snapshots
    from app.paper_trading.cities import resolve_city
    from app.paper_trading.weather_peaks import (
        _as_datetime, bracket_label, city_timezone, parse_temperature_market, recommendation_kinds,
    )

    wanted = set(cities)
    kinds = recommendation_kinds()
    events: Dict[tuple, Dict[str, Any]] = {}
    for m in get_market_snapshots(now=now, include_resolved=False):
        parsed = parse_temperature_market(m["market_name"], _as_datetime(m.get("end_date")), now)
        if not parsed or parsed.kind not in kinds:
            continue
        city = resolve_city(parsed.city)
        tz = city_timezone(city)
        if city not in wanted or tz is None or parsed.local_date != now.astimezone(tz).date():
            continue
        label = bracket_label(m["market_name"])
        b = _bracket(label)
        if b is None or m.get("price_yes") is None:
            continue
        ev = events.setdefault((city, parsed.kind), {"city": city, "kind": parsed.kind, "tz": tz,
                                                     "date": parsed.local_date, "station": m.get("resolution_station"),
                                                     "brackets": []})
        ev["station"] = ev["station"] or m.get("resolution_station")
        ev["brackets"].append({"label": label, "range": b, "price": float(m["price_yes"]),
                               "token": m.get("yes_token_id"), "market_id": m["market_id"]})
    return list(events.values())


def _neighbor(event: Dict[str, Any], fav: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Bracket yang menang bila hasil bergeser satu langkah: di atas (tertinggi) / di bawah (terendah)."""
    lo, hi, _ = fav["range"]
    for b in event["brackets"]:
        blo, bhi, _ = b["range"]
        if event["kind"] == "highest" and hi != math.inf and blo == hi + 1:
            return b
        if event["kind"] == "lowest" and lo != -math.inf and bhi == lo - 1:
            return b
    return None


def price_momentum(token: Optional[str], now: datetime, minutes: int = 30) -> Optional[Dict[str, float]]:
    """Harga token sekarang vs `minutes` lalu dari riwayat CLOB (per 5 menit)."""
    if not token:
        return None
    from app.paper_trading.live_market_data import _cached, _http

    def load():
        start = int((now - timedelta(minutes=minutes + 15)).timestamp())
        data = json.loads(_http(f"https://clob.polymarket.com/prices-history?market={token}&startTs={start}"
                                f"&endTs={int(now.timestamp())}&fidelity=5") or "{}")
        return sorted(data.get("history", []), key=lambda p: p["t"])

    try:
        history = _cached(f"momentum:{token}:{int(now.timestamp() // 120)}", 120, load)
    except Exception as err:
        logger.warning("Gagal mengambil riwayat harga %s: %s", token, err)
        return None
    if len(history) < 2:
        return None
    cutoff = now.timestamp() - minutes * 60
    before = next((p["p"] for p in reversed(history) if p["t"] <= cutoff), history[0]["p"])
    return {"before": float(before), "now": float(history[-1]["p"]), "change": float(history[-1]["p"]) - float(before)}


def observation(event: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
    """{extreme, current, rate, final, decimal, unit, official_hint} dari stasiun resolusi market."""
    unit = next((b["range"][2] for b in event["brackets"]), "C")
    if event["city"] == "Hong Kong" and event["kind"] == "highest":
        from app.paper_trading.hko_alerts import hko_status
        status = hko_status(now=now)
        if not status:
            return None
        return {"extreme": status["max"], "current": status["temp"], "rate": status.get("rate"),
                "final": bool(status.get("final_ok")), "decimal": True, "unit": "C",
                "official_hint": status.get("official_hint"), "final_note": f"sebelum {settings.HKO_FINAL_HOUR}:00 HKT"}
    station = event.get("station")
    if not station:
        return None
    from app.paper_trading.live_market_data import station_report
    from app.paper_trading.weather_peaks import recommendation_window

    report = station_report(station, event["tz"], unit, now=now, city=event["city"])
    if not report:
        return None
    extreme = report.get("max") if event["kind"] == "highest" else report.get("min")
    window = recommendation_window(event["city"], event["kind"], event["date"])
    local = now.astimezone(event["tz"])
    if event["kind"] == "highest":
        final = bool(window and now >= window.peak_end + timedelta(hours=2) and (report.get("trend") or 0) <= 0)
    else:
        final = local.hour >= 23  # suhu terendah hari kalender bisa turun sampai tengah malam
    return {"extreme": extreme, "current": report.get("current"), "rate": report.get("trend"), "final": final,
            "decimal": station == "HKO", "unit": unit, "official_hint": None, "final_note": "belum final"}


def indicators(event: Dict[str, Any], fav: Dict[str, Any], neighbor: Optional[Dict[str, Any]],
               obs: Optional[Dict[str, Any]], momentum: Optional[Dict[str, float]]) -> List[str]:
    """Daftar indikasi berbalik untuk bracket favorit (kosong = aman)."""
    found: List[str] = []
    lo, hi, unit = fav["range"]
    kind = event["kind"]
    if obs and obs.get("extreme") is not None:
        x, cur, rate = obs["extreme"], obs.get("current"), obs.get("rate") or 0.0
        # titik pindah bracket: HKO 0.1°C → batas atas + 1; bacaan bulat → batas atas + 0.5
        if kind == "highest" and hi != math.inf:
            flip = hi + 1 if obs["decimal"] else hi + 0.5
            if x >= flip - 1e-9:
                found.append(f"data stasiun sudah {x:g}°{unit}, di atas bracket {fav['label']}")
            elif not obs["final"]:
                distance = flip - x
                holding = cur is not None and cur >= x - 0.3
                if distance <= (0.3 if obs["decimal"] else 0.5) + 1e-9 and (rate > 0.1 or holding):
                    trend = f", suhu {'naik ' + format(rate, '+.1f') + '°/jam' if rate > 0.1 else 'bertahan dekat max'}"
                    found.append(f"max {x:g}°{unit} tinggal {distance:.1f}° dari batas {fav['label']}{trend}")
        if kind == "lowest" and lo != -math.inf:
            flip = lo if obs["decimal"] else lo - 0.5
            if x < flip - 1e-9:
                found.append(f"data stasiun sudah {x:g}°{unit}, di bawah bracket {fav['label']}")
            elif not obs["final"] and cur is not None and cur - flip <= (0.3 if obs["decimal"] else 0.5) and rate < -0.1:
                found.append(f"suhu {cur:g}°{unit} turun {rate:+.1f}°/jam, dekat batas bawah {fav['label']}")
        hint = obs.get("official_hint")
        if hint is not None and kind == "highest" and hi != math.inf and hint >= hi + 1:
            found.append(f"prakiraan resmi HKO ±{hint:.0f}°C, di atas bracket {fav['label']}")
    if neighbor and momentum and momentum["change"] >= settings.REVERSAL_MOMENTUM:
        found.append(f"bracket {neighbor['label']} naik {momentum['before'] * 100:.0f}¢ → {momentum['now'] * 100:.0f}¢ dalam 30 menit")
    return found


def format_alert(event: Dict[str, Any], fav: Dict[str, Any], neighbor: Optional[Dict[str, Any]],
                 found: List[str], obs: Optional[Dict[str, Any]]) -> str:
    from app.paper_trading.recommendation_alerts import city_hashtag

    kind = "max" if event["kind"] == "highest" else "min"
    lines = [f"🔄 WASPADA BERBALIK · {city_hashtag(event['city'])} {kind} · bracket {fav['label']} di {fav['price'] * 100:.0f}¢",
             "Indikasi:"] + [f"• {x}" for x in found]
    if neighbor:
        lines.append(f"Bracket sebelah: {neighbor['label']} di {neighbor['price'] * 100:.1f}¢")
    if obs and not obs.get("final"):
        lines.append(f"Hasil belum final ({obs.get('final_note')}). Harga ≥90¢ belum tentu aman.")
    lines.append("Informasi, bukan saran finansial.")
    return "\n".join(lines)


def check_reversals(now: Optional[datetime] = None) -> List[str]:
    """Satu siklus: kirim alert waspada berbalik yang baru. Kembalikan teks yang terkirim."""
    if not settings.REVERSAL_ALERTS:
        return []
    from app.paper_service import get_city_volume_summary
    from app.paper_trading.telegram import send_telegram_message

    now = now or datetime.now(timezone.utc)
    top = [r["city"] for r in get_city_volume_summary(limit=settings.TELEGRAM_RECOMMENDATION_TOP_CITIES or 7, now=now)]
    cities = list(dict.fromkeys(["Hong Kong"] + top))
    sent: List[str] = []
    for event in today_events(now, cities):
        fav = max(event["brackets"], key=lambda b: b["price"])
        if fav["price"] < settings.REVERSAL_MIN_PRICE:
            continue
        key_station = event["city"][:20]
        kind = f"reversal_{event['kind']}"[:20]
        value = fav["range"][1] if event["kind"] == "highest" else fav["range"][0]
        if value in (math.inf, -math.inf):
            value = 999 if value == math.inf else -999
        local_date = event["date"].isoformat()
        db = get_db_session()
        try:
            done = db.query(StationAlert).filter_by(station=key_station, kind=kind, local_date=local_date,
                                                    value=Decimal(str(value))).first()
        finally:
            db.close()
        if done:
            continue
        try:
            neighbor = _neighbor(event, fav)
            obs = observation(event, now)
            momentum = price_momentum(neighbor["token"], now) if neighbor else None
            found = indicators(event, fav, neighbor, obs, momentum)
        except Exception as err:
            logger.warning("Gagal mengevaluasi %s %s: %s", event["city"], event["kind"], err)
            continue
        if not found:
            continue
        text = format_alert(event, fav, neighbor, found, obs)
        result = send_telegram_message(text)
        if not result.get("success"):
            logger.warning("Alert waspada berbalik tidak terkirim: %s", result.get("error"))
            continue
        db = get_db_session()
        try:
            db.add(StationAlert(station=key_station, kind=kind, local_date=local_date, value=Decimal(str(value)),
                                sent_at=now))
            db.commit()
        finally:
            db.close()
        sent.append(text)
    return sent


def run_reversal_watch() -> int:
    """Dipanggil dari loop collector; tidak pernah melempar exception."""
    try:
        return len(check_reversals())
    except Exception as err:
        logger.error("Gagal menjalankan waspada berbalik: %s", err, exc_info=True)
        return 0
