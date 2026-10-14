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

Hanya market volume besar & likuid: volume event hari itu ≥ REVERSAL_MIN_VOLUME, dan order book favorit
spread ≤ REVERSAL_MAX_SPREAD dengan kedalaman bid ≥ REVERSAL_MIN_DEPTH_USD (posisi bisa dijual).

Setiap favorit ≥90¢ yang lolos filter dicatat di reversal_watches lalu diikuti:
- "🔁 BENAR BERBALIK" dikirim saat harga favorit jatuh < REVERSAL_CONFIRM_PRICE dan bracket lain memimpin
  (dengan keterangan apakah warning sudah dikirim sebelumnya);
- setelah resolve: outcome held / reversed → statistik ketepatan warning (/berbalik, dashboard).

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
from app.paper_trading.models import MarketLatest, ReversalWatch, StationAlert

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

    wanted = set(cities) if cities is not None else None
    kinds = recommendation_kinds()
    events: Dict[tuple, Dict[str, Any]] = {}
    for m in get_market_snapshots(now=now, include_resolved=False):
        parsed = parse_temperature_market(m["market_name"], _as_datetime(m.get("end_date")), now)
        if not parsed or parsed.kind not in kinds:
            continue
        city = resolve_city(parsed.city)
        tz = city_timezone(city)
        if (wanted is not None and city not in wanted) or tz is None or parsed.local_date != now.astimezone(tz).date():
            continue
        label = bracket_label(m["market_name"])
        b = _bracket(label)
        if b is None or m.get("price_yes") is None:
            continue
        ev = events.setdefault((city, parsed.kind), {"city": city, "kind": parsed.kind, "tz": tz,
                                                     "date": parsed.local_date, "station": m.get("resolution_station"),
                                                     "brackets": [], "volume": 0.0})
        ev["station"] = ev["station"] or m.get("resolution_station")
        ev["volume"] += float(m.get("volume") or 0)
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


def liquidity(fav: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """{spread, bid, bid_depth_usd} order book favorit, atau None bila tidak likuid / tidak ada data."""
    from app.paper_trading.live_market_data import fetch_order_books

    book = fetch_order_books([fav["token"]]).get(str(fav["token"])) if fav.get("token") else None
    if not book or book.get("ask") is None or book.get("bid") is None:
        return None
    spread = round(book["ask"] - book["bid"], 4)
    depth = float(book.get("bid_depth_usd") or 0)
    if spread > settings.REVERSAL_MAX_SPREAD + 1e-9 or depth < settings.REVERSAL_MIN_DEPTH_USD:
        return None
    return {"spread": spread, "bid": book["bid"], "bid_depth_usd": depth}


def big_liquid(event: Dict[str, Any], fav: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Filter volume besar & likuid; kembalikan info likuiditas favorit bila lolos."""
    if event.get("volume", 0) < settings.REVERSAL_MIN_VOLUME:
        return None
    return liquidity(fav)


def _get_watch(db, event: Dict[str, Any], fav: Dict[str, Any]) -> Optional[ReversalWatch]:
    return db.query(ReversalWatch).filter_by(city=event["city"][:50], kind=event["kind"],
                                            local_date=event["date"].isoformat(), fav_label=fav["label"][:50]).first()


def _legacy_alert_sent(db, event: Dict[str, Any], fav: Dict[str, Any]) -> bool:
    """Warning yang terkirim sebelum reversal_watches ada (dedupe lama di station_alerts)."""
    value = fav["range"][1] if event["kind"] == "highest" else fav["range"][0]
    if value in (math.inf, -math.inf):
        value = 999 if value == math.inf else -999
    return db.query(StationAlert).filter_by(station=event["city"][:20], kind=f"reversal_{event['kind']}"[:20],
                                            local_date=event["date"].isoformat(),
                                            value=Decimal(str(value))).first() is not None


def check_reversals(now: Optional[datetime] = None) -> List[str]:
    """Satu siklus: catat favorit ≥90¢, kirim warning baru, lalu konfirmasi yang benar berbalik."""
    if not settings.REVERSAL_ALERTS:
        return []
    from app.paper_trading.telegram import send_telegram_message

    now = now or datetime.now(timezone.utc)
    sent: List[str] = []
    for event in today_events(now, None):
        fav = max(event["brackets"], key=lambda b: b["price"])
        if fav["price"] < settings.REVERSAL_MIN_PRICE:
            continue
        db = get_db_session()
        try:
            watch = _get_watch(db, event, fav)
            legacy = watch is None and _legacy_alert_sent(db, event, fav)
        finally:
            db.close()
        if watch is None:
            try:
                liq = big_liquid(event, fav)
            except Exception as err:
                logger.warning("Gagal cek likuiditas %s %s: %s", event["city"], event["kind"], err)
                continue
            if liq is None:
                continue  # volume kecil / tidak likuid: tidak dipantau
            db = get_db_session()
            try:
                watch = ReversalWatch(
                    city=event["city"][:50], kind=event["kind"], local_date=event["date"].isoformat(),
                    fav_label=fav["label"][:50], fav_market_id=fav["market_id"],
                    peak_price=Decimal(str(round(fav["price"], 4))), last_price=Decimal(str(round(fav["price"], 4))),
                    event_volume=Decimal(str(round(event.get("volume", 0), 2))),
                    brackets=json.dumps([[b["market_id"], b["label"]] for b in event["brackets"]]),
                    first_seen_at=now, warned_at=now if legacy else None,
                    warning="(warning terkirim sebelum pelacakan aktif)" if legacy else None)
                db.add(watch)
                db.commit()
                db.refresh(watch)
            finally:
                db.close()
        elif fav["price"] > float(watch.peak_price):
            db = get_db_session()
            try:
                row = db.get(ReversalWatch, watch.id)
                row.peak_price = Decimal(str(round(fav["price"], 4)))
                db.commit()
            finally:
                db.close()
        if watch.warned_at is not None:
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
            row = db.get(ReversalWatch, watch.id)
            row.warned_at, row.warning = now, "; ".join(found)[:2000]
            db.commit()
        finally:
            db.close()
        sent.append(text)
    sent += follow_up(now)
    return sent


def _event_prices(db, brackets: List[List[str]]) -> Dict[str, Optional[float]]:
    ids = [m for m, _ in brackets]
    rows = db.query(MarketLatest).filter(MarketLatest.market_id.in_(ids)).all() if ids else []
    return {r.market_id: float(r.price_yes) if r.price_yes is not None else None for r in rows}


def format_flip(w: ReversalWatch, fav_price: float, leader_label: str, leader_price: float, at_resolution: bool) -> str:
    from app.paper_trading.recommendation_alerts import city_hashtag

    kind = "max" if w.kind == "highest" else "min"
    when = "saat resolve" if at_resolution else "sekarang"
    lines = [f"🔁 BENAR BERBALIK · {city_hashtag(w.city)} {kind} {w.local_date}",
             f"Bracket {w.fav_label} sempat {float(w.peak_price) * 100:.0f}¢ → {when} {fav_price * 100:.0f}¢",
             f"Pemimpin baru: {leader_label} di {leader_price * 100:.0f}¢"]
    if w.warned_at is not None:
        warned = _aware(w.warned_at)
        lines.append(f"✅ Warning waspada berbalik sudah dikirim {warned.astimezone(timezone.utc):%H:%M} UTC"
                     + (f" — {w.warning}" if w.warning else ""))
    else:
        lines.append("⚠️ Tanpa warning sebelumnya (indikator tidak menangkap pembalikan ini)")
    lines.append("Informasi, bukan saran finansial.")
    return "\n".join(lines)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def follow_up(now: datetime) -> List[str]:
    """Konfirmasi benar berbalik (harga live) dan isi hasil akhir setelah resolve."""
    from app.paper_trading.models import MarketResolution
    from app.paper_trading.telegram import send_telegram_message

    sent: List[str] = []
    since = (now - timedelta(days=3)).date().isoformat()
    db = get_db_session()
    try:
        watches = (db.query(ReversalWatch)
                   .filter(ReversalWatch.resolved_at.is_(None), ReversalWatch.local_date >= since).all())
        due = [w for w in watches if w.local_date < now.date().isoformat()
               and (w.checked_at is None or now - _aware(w.checked_at) >= timedelta(minutes=30))]
        if due:
            try:
                from app.market_collector.collector import sync_markets_by_condition_ids
                sync_markets_by_condition_ids([m for w in due for m, _ in json.loads(w.brackets)])
            except Exception as err:
                logger.warning("Gagal sinkron resolusi market berbalik: %s", err)
        for w in watches:
            brackets = json.loads(w.brackets)
            prices = _event_prices(db, brackets)
            labels = {m: label for m, label in brackets}
            fav_price = prices.get(w.fav_market_id)
            resolved = {}
            if w in due:
                w.checked_at = now
                resolved = {r.market_id: r.winning_outcome for r in
                            db.query(MarketResolution).filter(MarketResolution.market_id.in_(list(labels)))}
            fav_outcome = resolved.get(w.fav_market_id)
            winner = next((labels[m] for m, o in resolved.items() if o == "YES"), None)
            if fav_price is not None:
                w.last_price = Decimal(str(round(fav_price, 4)))
            at_resolution = fav_outcome is not None
            if at_resolution:
                w.outcome = "held" if fav_outcome == "YES" else "reversed"
                w.winning_bracket = winner
                w.resolved_at = now
                fav_price = 1.0 if fav_outcome == "YES" else 0.0
            if w.flipped_at is not None or fav_price is None or fav_price >= settings.REVERSAL_CONFIRM_PRICE:
                continue
            others = [(labels[m], p) for m, p in prices.items() if m != w.fav_market_id and p is not None]
            if winner:
                leader = (winner, 1.0)
            else:
                leader = max(others, key=lambda x: x[1], default=None)
            if leader is None or (not at_resolution and leader[1] <= fav_price):
                continue
            text = format_flip(w, fav_price, leader[0], leader[1], at_resolution)
            w.flipped_at, w.flip_detail = now, f"{w.fav_label} {fav_price * 100:.0f}¢ → {leader[0]} {leader[1] * 100:.0f}¢"
            result = send_telegram_message(text)
            if result.get("success"):
                sent.append(text)
            else:
                logger.warning("Alert benar berbalik tidak terkirim: %s", result.get("error"))
        db.commit()
    finally:
        db.close()
    return sent


def reversal_history(days: Optional[int] = None, limit: int = 30, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Favorit ≥90¢ yang dipantau + ringkasan ketepatan warning."""
    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    try:
        q = db.query(ReversalWatch)
        if days:
            q = q.filter(ReversalWatch.local_date >= (now - timedelta(days=days)).date().isoformat())
        rows = q.order_by(ReversalWatch.local_date.desc(), ReversalWatch.first_seen_at.desc()).all()
    finally:
        db.close()

    def reversed_(w):
        return w.outcome == "reversed" or (w.outcome is None and w.flipped_at is not None)

    warned = [w for w in rows if w.warned_at is not None]
    rev = [w for w in rows if reversed_(w)]
    decided_warned = [w for w in warned if w.outcome is not None]
    summary = {
        "tracked": len(rows), "reversed": len(rev), "warned": len(warned),
        "warned_reversed": sum(1 for w in decided_warned if w.outcome == "reversed"),
        "warned_decided": len(decided_warned),
        "reversed_warned": sum(1 for w in rev if w.warned_at is not None),
        "pending": sum(1 for w in rows if w.outcome is None),
    }
    items = [{
        "city": w.city, "kind": w.kind, "date": w.local_date, "fav": w.fav_label,
        "peak": float(w.peak_price), "last": float(w.last_price) if w.last_price is not None else None,
        "volume": float(w.event_volume) if w.event_volume is not None else None,
        "warned_at": _aware(w.warned_at).isoformat() if w.warned_at else None, "warning": w.warning,
        "flipped_at": _aware(w.flipped_at).isoformat() if w.flipped_at else None, "flip": w.flip_detail,
        "outcome": w.outcome, "winner": w.winning_bracket,
        "verdict": _verdict(w),
    } for w in rows[:limit]]
    return {"summary": summary, "items": items}


def _verdict(w: ReversalWatch) -> str:
    """Penilaian warning: tepat / alarm palsu / terlewat / aman / menunggu."""
    reversed_ = w.outcome == "reversed" or (w.outcome is None and w.flipped_at is not None)
    if w.warned_at is not None:
        if reversed_:
            return "warning tepat"
        return "alarm palsu" if w.outcome == "held" else "menunggu"
    if reversed_:
        return "terlewat"
    return "aman" if w.outcome == "held" else "menunggu"


def format_reversal_history(days: Optional[int] = None) -> str:
    """/berbalik: favorit ≥90¢ yang dipantau, warning, dan apakah benar berbalik."""
    data = reversal_history(days=days, limit=15)
    s = data["summary"]
    label = f"{days} hari" if days else "semua waktu"
    lines = [f"🔄 *Waspada berbalik · riwayat* ({label})",
             f"Favorit ≥{settings.REVERSAL_MIN_PRICE * 100:.0f}¢ dipantau: {s['tracked']} · benar berbalik: {s['reversed']} · "
             f"warning: {s['warned']} · menunggu hasil: {s['pending']}"]
    if s["warned_decided"]:
        lines.append(f"Ketepatan warning: {s['warned_reversed']}/{s['warned_decided']} warning benar berbalik")
    if s["reversed"]:
        lines.append(f"Pembalikan yang didahului warning: {s['reversed_warned']}/{s['reversed']}")
    icons = {"warning tepat": "✅", "alarm palsu": "🟡", "terlewat": "❌", "aman": "🟢", "menunggu": "⏳"}
    for it in data["items"]:
        kind = "max" if it["kind"] == "highest" else "min"
        extra = f" · {it['flip']}" if it["flip"] else ""
        winner = f" · menang {it['winner']}" if it["winner"] else ""
        lines.append(f"{icons.get(it['verdict'], '•')} {it['date']} {it['city']} {kind} {it['fav']} "
                     f"(puncak {it['peak'] * 100:.0f}¢) — {it['verdict']}{extra}{winner}")
    if not data["items"]:
        lines.append("Belum ada favorit ≥90¢ di market volume besar & likuid yang tercatat.")
    lines.append(f"Filter: volume event ≥ ${settings.REVERSAL_MIN_VOLUME:,.0f}, spread ≤ {settings.REVERSAL_MAX_SPREAD * 100:.0f}¢, "
                 f"kedalaman bid ≥ ${settings.REVERSAL_MIN_DEPTH_USD:,.0f}. `/berbalik 7` untuk 7 hari.")
    return "\n".join(lines)


def run_reversal_watch() -> int:
    """Dipanggil dari loop collector; tidak pernah melempar exception."""
    try:
        return len(check_reversals())
    except Exception as err:
        logger.error("Gagal menjalankan waspada berbalik: %s", err, exc_info=True)
        return 0
