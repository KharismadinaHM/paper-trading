"""
Auto paper trader — membeli OTOMATIS di akun PAPER berdasarkan model, dengan risk engine.
Tidak pernah mengirim order ke Polymarket.

Keputusan selalu: beli sisi/bracket X hanya jika  P_model(X) − (VWAP ask + fee taker) ≥ edge minimum,
memakai order book live (level ask disusuri sesuai ukuran order) dan biaya taker per market.

Strategi:
- btc (auto_btc_v1): market "Bitcoin Up or Down" per jam (candle 1H BTC/USDT Binance). Pada menit
  AUTOTRADE_BTC_WINDOW, P(Up) = Φ(ln(S/Open) / (σ_1m·√menit tersisa)), σ dari 120 candle 1 menit.
- weather (auto_weather_v1): gabungan waktu + observasi + harga. Event di jendela rekomendasi jam
  puncak; perkiraan suhu akhir dari stasiun resolusi (HKO untuk Hong Kong, METAR + prakiraan
  terkoreksi untuk kota lain); P(bracket) dari distribusi normal di sekitar perkiraan, dipotong di
  angka yang sudah terukur. Opsional wajib sepakat dengan favorit pasar.

Risk engine: maks per order, maks pembelian per hari (WIB), stop jika rugi terealisasi hari itu ≥
batas, maks total posisi terbuka, maks harga, maks spread, satu entri per market, kill switch.
"""
import json
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import func

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.models import (
    AutotradeDecision, AutotradeLimitOrder, AutotradeSignal, AutotradeState, PaperPosition, PaperTrade,
    PaperTradeStatus,
)

logger = get_logger("autotrader")

STRATEGY_VERSIONS = {
    "btc": "auto_btc_v1", "btc15": "auto_btc15_v1", "btc5": "auto_btc5_v1", "maker_btc": "auto_maker_btc_v1",
    "maker_btc15": "auto_maker_btc15_v1", "maker_btc5": "auto_maker_btc5_v1",
    "eth": "auto_eth_v1", "eth15": "auto_eth15_v1", "maker_eth": "auto_maker_eth_v1", "maker_eth15": "auto_maker_eth15_v1",
    "weather": "auto_weather_v1", "weather_post": "auto_weather_post_v1",
}
BINANCE = "https://data-api.binance.vision"
ET = ZoneInfo("America/New_York")


def _local_day(now: datetime) -> str:
    return now.astimezone(ZoneInfo(settings.NOTIFY_TIMEZONE)).date().isoformat()


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def taker_fee(price: float, rate: float) -> float:
    return rate * price * (1 - price)


# --- State --------------------------------------------------------------------------------

def _get_state(key: str) -> Optional[str]:
    db = get_db_session()
    try:
        row = db.get(AutotradeState, key)
        return row.value if row else None
    finally:
        db.close()


def _set_state(key: str, value: str, now: Optional[datetime] = None) -> None:
    db = get_db_session()
    try:
        row = db.get(AutotradeState, key)
        if row is None:
            row = AutotradeState(key=key)
            db.add(row)
        row.value, row.updated_at = value, now or datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def is_enabled() -> bool:
    value = _get_state("enabled")
    return settings.AUTOTRADE_ENABLED if value is None else value == "1"


def set_enabled(enabled: bool) -> None:
    _set_state("enabled", "1" if enabled else "0")


def enabled_strategies() -> List[str]:
    return [s for s in (x.strip().lower() for x in str(cfg("STRATEGIES")).split(",")) if s in STRATEGY_VERSIONS]



# --- Konfigurasi yang bisa diubah dari dashboard ------------------------------------------
#
# Nilai default dari .env (AUTOTRADE_*); override disimpan di autotrade_state["config"] dan langsung
# berlaku tanpa restart. (tipe, min, max, label)
EDITABLE_CONFIG: Dict[str, Tuple[str, float, float, str]] = {
    "ORDER_USD": ("float", 1, 1000, "Ukuran per order ($)"),
    "MAX_DAILY_USD": ("float", 1, 100000, "Maks pembelian per hari ($)"),
    "MAX_DAILY_LOSS": ("float", 1, 100000, "Stop hari itu bila rugi terealisasi ≥ ($)"),
    "MAX_OPEN_USD": ("float", 1, 100000, "Maks total posisi terbuka ($)"),
    "MAX_PRICE": ("float", 0.05, 0.99, "Harga beli maksimum (0–1)"),
    "MAX_SPREAD": ("float", 0.01, 0.5, "Spread order book maksimum (0–1)"),
    "BTC_MIN_EDGE": ("float", 0, 0.5, "Edge minimum crypto (BTC/ETH) taker (0–1)"),
    "BTC_MIN_PRICE": ("float", 0, 0.9, "Harga beli minimum crypto (BTC/ETH), taker & maker (0–1)"),
    "SLIPPAGE": ("float", 0, 0.1, "Simulasi slippage per share (0.01 = 1¢)"),
    "WEATHER_MIN_EDGE": ("float", 0, 0.5, "Edge minimum cuaca (0–1)"),
    "MAKER_MARGIN": ("float", 0.01, 0.3, "Maker: harga limit = P − margin"),
    "MAKER_MIN_EDGE": ("float", 0, 0.3, "Maker: edge minimum"),
    "WEATHER_REQUIRE_AGREEMENT": ("bool", 0, 1, "Cuaca: wajib sepakat dengan favorit pasar"),
    "STRATEGIES": ("strategies", 0, 0, "Strategi aktif"),
}
_config_cache: Dict[str, Any] = {"at": 0.0, "values": {}}


def _config_overrides() -> Dict[str, Any]:
    import time as _time
    if _time.monotonic() - _config_cache["at"] < 5:
        return _config_cache["values"]
    raw = _get_state("config")
    try:
        values = json.loads(raw) if raw else {}
    except ValueError:
        values = {}
    _config_cache.update(at=_time.monotonic(), values=values)
    return values


def cfg(name: str) -> Any:
    """Nilai aturan auto trader: override dashboard bila ada, selain itu AUTOTRADE_<name> dari .env."""
    overrides = _config_overrides()
    if name in overrides:
        return overrides[name]
    value = getattr(settings, f"AUTOTRADE_{name}")
    return float(value) if isinstance(value, Decimal) else value


def _validate(name: str, value: Any) -> Any:
    kind, lo, hi, label = EDITABLE_CONFIG[name]
    if kind == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "ya", "yes", "on")
        return bool(value)
    if kind == "strategies":
        items = value if isinstance(value, list) else str(value).split(",")
        items = [x.strip().lower() for x in items if str(x).strip()]
        unknown = [x for x in items if x not in STRATEGY_VERSIONS]
        if unknown:
            raise ValueError(f"Strategi tidak dikenal: {', '.join(unknown)}")
        return ",".join(items)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}: harus berupa angka")
    if not (lo <= number <= hi) or math.isnan(number):
        raise ValueError(f"{label}: harus antara {lo:g} dan {hi:g}")
    return number


def set_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Validasi lalu simpan override. Nilai None menghapus override (kembali ke .env)."""
    unknown = [k for k in updates if k not in EDITABLE_CONFIG]
    if unknown:
        raise ValueError(f"Pengaturan tidak dikenal: {', '.join(unknown)}")
    values = dict(_config_overrides())
    for name, value in updates.items():
        if value is None or value == "":
            values.pop(name, None)
        else:
            values[name] = _validate(name, value)
    _set_state("config", json.dumps(values))
    _config_cache["at"] = 0.0
    return get_config()


def reset_config() -> Dict[str, Any]:
    _set_state("config", "{}")
    _config_cache["at"] = 0.0
    return get_config()


def get_config() -> Dict[str, Dict[str, Any]]:
    overrides = _config_overrides()
    out = {}
    for name, (kind, lo, hi, label) in EDITABLE_CONFIG.items():
        default = getattr(settings, f"AUTOTRADE_{name}")
        default = float(default) if isinstance(default, Decimal) else default
        out[name] = {"value": cfg(name), "default": default, "overridden": name in overrides,
                     "type": kind, "min": lo, "max": hi, "label": label}
    out["STRATEGIES"]["options"] = list(STRATEGY_VERSIONS)
    return out

# --- Risk engine --------------------------------------------------------------------------

def today_summary(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Pembelian hari ini (WIB), PnL terealisasi hari ini dan posisi terbuka milik auto trader."""
    now = now or datetime.now(timezone.utc)
    day = _local_day(now)
    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    day_start = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    versions = list(STRATEGY_VERSIONS.values())
    db = get_db_session()
    try:
        spent = db.query(func.coalesce(func.sum(AutotradeDecision.size_usd), 0)).filter(
            AutotradeDecision.local_day == day, AutotradeDecision.status == "filled").scalar()
        count = db.query(AutotradeDecision).filter(
            AutotradeDecision.local_day == day, AutotradeDecision.status == "filled").count()
        realized = db.query(func.coalesce(func.sum(PaperTrade.net_pnl), 0)).filter(
            PaperTrade.strategy_version.in_(versions), PaperTrade.closed_at >= day_start).scalar()
        open_usd = db.query(func.coalesce(func.sum(PaperPosition.position_size), 0)).filter(
            PaperPosition.strategy_version.in_(versions), PaperPosition.shares > 0).scalar()
    finally:
        db.close()
    return {"day": day, "spent": float(spent or 0), "trades": count, "realized_pnl": float(realized or 0),
            "open_usd": float(open_usd or 0)}


def risk_check(size: float, now: Optional[datetime] = None) -> Tuple[bool, Optional[str]]:
    t = today_summary(now)
    if t["realized_pnl"] <= -float(cfg("MAX_DAILY_LOSS")):
        return False, f"stop harian: rugi terealisasi hari ini ${-t['realized_pnl']:.2f}"
    if t["spent"] + size > float(cfg("MAX_DAILY_USD")) + 1e-9:
        return False, f"batas harian ${float(cfg("MAX_DAILY_USD")):.0f} tercapai"
    if t["open_usd"] + size > float(cfg("MAX_OPEN_USD")) + 1e-9:
        return False, f"batas posisi terbuka ${float(cfg("MAX_OPEN_USD")):.0f} tercapai"
    return True, None


def already_decided(decision_key: str) -> bool:
    db = get_db_session()
    try:
        return db.query(AutotradeDecision.id).filter_by(decision_key=decision_key).first() is not None
    finally:
        db.close()


# --- Eksekusi -----------------------------------------------------------------------------

def _record(decision: Dict[str, Any], status: str, reason: Optional[str], now: datetime) -> None:
    db = get_db_session()
    try:
        db.add(AutotradeDecision(
            decision_key=decision["key"], strategy=decision["strategy"], market_id=decision["market_id"],
            label=decision["label"][:255], side=decision["side"], model_prob=Decimal(str(round(decision["prob"], 4))),
            price=Decimal(str(round(decision["price"], 6))), fee=Decimal(str(round(decision["fee"], 6))),
            edge=Decimal(str(round(decision["edge"], 4))), size_usd=Decimal(str(decision["size"])),
            status=status, reason=reason, local_day=_local_day(now), created_at=now,
            features=json.dumps({**(decision.get("features") or {}), "detail": decision.get("detail")}, default=str)
            if decision.get("features") or decision.get("detail") else None))
        db.commit()
    finally:
        db.close()


def log_signal(decision: Dict[str, Any], signal_key: str, skip_reason: Optional[str], now: datetime) -> bool:
    """Catat sampel sinyal (sekali per signal_key). Gagal mencatat tidak boleh mengganggu trading."""
    try:
        db = get_db_session()
        try:
            if db.query(AutotradeSignal.id).filter_by(signal_key=signal_key).first():
                return False
            db.add(AutotradeSignal(
                signal_key=signal_key[:255], strategy=decision["strategy"], market_id=decision["market_id"],
                label=(decision.get("label") or "")[:255], side=decision["side"],
                model_prob=Decimal(str(round(decision["prob"], 4))), price=Decimal(str(round(decision["price"], 6))),
                fee=Decimal(str(round(decision["fee"], 6))), edge=Decimal(str(round(decision["edge"], 4))),
                action="skipped" if skip_reason else "traded", skip_reason=skip_reason,
                features=json.dumps(decision.get("features") or {}, default=str),
                local_day=_local_day(now), created_at=now))
            db.commit()
            return True
        finally:
            db.close()
    except Exception as err:
        logger.warning("Gagal mencatat sinyal %s: %s", signal_key, err)
        return False


def notify(text: str) -> Dict[str, Any]:
    from app.paper_trading.telegram import send_telegram_message

    result = send_telegram_message(text, chat_id=settings.TELEGRAM_AUTOTRADE_CHAT_ID or None)
    if not result.get("success"):
        logger.warning("Notifikasi auto trade tidak terkirim: %s", result.get("error"))
    return result


def _reason_group(reason: str) -> str:
    """Kelompok alasan penolakan untuk dedupe notifikasi (angka dibuang)."""
    import re
    return re.sub(r"[\d$.,:%]+", "#", str(reason or ""))[:60]


def notify_rejection(decision: Dict[str, Any], reason: str, now: datetime) -> bool:
    """Kabari penolakan sekali per jenis alasan per hari (mis. saldo paper tidak cukup, batas harian)."""
    import hashlib
    raw = f"{_local_day(now)}:{_reason_group(reason)}"
    key = f"rej:{hashlib.sha1(raw.encode()).hexdigest()[:24]}"  # kolom key autotrade_state maks 50 karakter
    if _get_state(key):
        return False
    _set_state(key, "1", now)
    hint = ""
    if "balance" in reason.lower() or "saldo" in reason.lower():
        hint = "\nTambah saldo paper lewat tombol Deposit di dashboard."
    notify(f"⚠️ AUTO TRADE DITOLAK (paper) · {strategy_header(decision['strategy'])}\n{decision['title']}\n"
           f"Alasan: {reason}\n"
           f"Sinyal: {decision.get('outcome') or decision['side']} · model {decision['prob'] * 100:.0f}% vs biaya "
           f"{(decision['price'] + decision['fee']) * 100:.1f}¢{hint}\n"
           "(Penolakan jenis ini hanya dikabarkan sekali per hari.)")
    return True


def send_test_notification() -> str:
    """/tesnotif: kirim pesan uji ke chat auto trade dan laporkan hasilnya."""
    target = settings.TELEGRAM_AUTOTRADE_CHAT_ID or "chat utama (TELEGRAM_CHAT_ID)"
    result = notify("🧪 Tes notifikasi auto trader — kalau pesan ini muncul, notifikasi auto trade masuk ke sini.")
    if result.get("success"):
        return f"✅ Pesan uji terkirim ke {target}."
    return (f"❌ Gagal mengirim ke {target}: {result.get('error')}\n"
            "Cek: bot sudah jadi anggota grup, ID grup diawali '-' (grup biasa -55…, supergroup -100…), "
            "dan container sudah di-restart setelah .env diubah.")


def execute(decision: Dict[str, Any], now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Risk check → paper order di harga order book (VWAP + fee) → catat → notifikasi."""
    from app.paper_service import create_paper_order

    now = now or datetime.now(timezone.utc)
    if already_decided(decision["key"]):
        return None
    ok, reason = risk_check(decision["size"], now)
    if not ok:
        _record(decision, "rejected", reason, now)
        logger.info("Auto trade ditolak (%s): %s", decision["key"], reason)
        notify_rejection(decision, reason, now)
        return None
    size = Decimal(str(decision["size"]))
    if not decision["strategy"].startswith("weather"):
        # Market BTC tidak dikumpulkan collector: segarkan snapshot agar tidak ditolak sebagai stale
        try:
            from app.market_collector.collector import sync_markets_by_condition_ids
            sync_markets_by_condition_ids([decision["market_id"]])
        except Exception as err:
            logger.warning("Gagal menyegarkan market %s: %s", decision["market_id"], err)
    try:
        order = create_paper_order(
            market_id=decision["market_id"], side=decision["side"], position_size=size,
            strategy_version=STRATEGY_VERSIONS[decision["strategy"]], now=now,
            execution_price=Decimal(str(round(decision["price"] + decision["fee"], 6))),
            risk_limits={"max_position_size": size, "max_market_exposure": size,
                         "max_total_exposure": Decimal(str(cfg("MAX_OPEN_USD")))},
            notify=False,
        )
    except ValueError as err:
        _record(decision, "rejected", str(err)[:500], now)
        logger.info("Auto trade ditolak paper engine (%s): %s", decision["key"], err)
        notify_rejection(decision, str(err)[:300], now)
        return None
    _record(decision, "filled", None, now)
    notify(format_decision(decision, order))
    return order


ASSET_ICONS = {"btc": "🟠", "eth": "🔷"}


def strategy_header(strategy: str) -> str:
    """Label pembeda di notifikasi: '🟠 BTC · 1 JAM', '🔷 ETH · 15 MENIT · MAKER', '🌡 CUACA'."""
    maker = strategy.startswith("maker_")
    info = BTC_SERIES.get(strategy.replace("maker_", ""))
    if info:
        label = (f"{ASSET_ICONS.get(info['asset'], '🪙')} {ASSETS[info['asset']]['label']} · "
                 f"{LENGTH_LABEL[info['minutes']].upper()}")
        return label + (" · MAKER" if maker else "")
    if strategy == "weather_post":
        return "🌡 CUACA · PASCA PUNCAK"
    if strategy.startswith("weather"):
        return "🌡 CUACA"
    return strategy.upper()


def format_decision(d: Dict[str, Any], order: Dict[str, Any]) -> str:
    side = d.get("outcome") or d["side"]
    lines = [
        f"🤖 AUTO BUY (paper) · {strategy_header(d['strategy'])}",
        f"{d['title']}",
        f"Beli {side} {float(order['shares']):,.2f} sh @ {d['price'] * 100:.1f}¢"
        + (f" (termasuk slippage {d['features']['slippage'] * 100:.1f}¢)" if (d.get("features") or {}).get("slippage") else "")
        + f" + fee {d['fee'] * 100:.2f}¢ = ${d['size']:.2f}",
        f"Model {d['prob'] * 100:.0f}% vs biaya {(d['price'] + d['fee']) * 100:.1f}¢ → edge {d['edge'] * 100:+.1f}¢",
    ]
    if d.get("detail"):
        lines.append(d["detail"])
    t = today_summary()
    lines.append(f"Hari ini: {t['trades']} trade · ${t['spent']:.2f}/{float(cfg("MAX_DAILY_USD")):.0f} · "
                 f"PnL terealisasi {t['realized_pnl']:+.2f}")
    return "\n".join(lines)


def _book_side(token: str, usd: float) -> Optional[Dict[str, float]]:
    from app.paper_trading.live_market_data import fetch_fee_rate, fetch_order_books, vwap_for_usd

    book = fetch_order_books([token]).get(str(token))
    if not book or book.get("ask") is None:
        return None
    fill = vwap_for_usd(book.get("asks") or [], usd)
    if fill is None:
        return None
    spread = (book["ask"] - book["bid"]) if book.get("bid") is not None else None
    rate = fetch_fee_rate(token)
    # Simulasi slippage: eksekusi nyata kalah cepat dari bot lain / harga bergeser saat order dikirim
    slippage = float(cfg("SLIPPAGE") or 0)
    price = min(0.99, fill["price"] + slippage)
    return {"price": price, "fee": taker_fee(price, rate), "spread": spread, "shares": fill["shares"],
            "book_price": fill["price"], "slippage": round(price - fill["price"], 4)}


# --- Strategi crypto Up/Down (BTC & ETH: 1 jam, 15 menit, 5 menit) -------------------------
#
# Seri 1 jam  : candle 1H <ASET>/USDT Binance (Up jika close ≥ open), slug <bitcoin|ethereum>-up-or-down-…-<jam>-et.
# Seri 15/5 menit: TWAP Chainlink <ASET>/USD di akhir rentang vs harga awal, slug <btc|eth>-updown-15m-<unix>.
#   Diuji pada 160 market BTC: "close Binance ≥ open Binance" cocok 93.8% dengan hasil resolusi (rata-rata
#   sepanjang rentang hanya 85.6%), jadi keduanya dimodelkan dengan harga akhir vs harga awal.
# Nama strategi tetap "btc_*" di beberapa fungsi (riwayat); seri ETH memakai mesin & aturan yang sama.

ASSETS = {
    "btc": {"symbol": "BTCUSDT", "hourly": "bitcoin", "label": "BTC"},
    "eth": {"symbol": "ETHUSDT", "hourly": "ethereum", "label": "ETH"},
}
ENTRY_WINDOWS = {60: lambda: settings.AUTOTRADE_BTC_WINDOW, 15: lambda: settings.AUTOTRADE_BTC15_WINDOW,
                 5: lambda: settings.AUTOTRADE_BTC5_WINDOW}
LENGTH_LABEL = {60: "1 jam", 15: "15 menit", 5: "5 menit"}


def _series(asset: str, minutes: int) -> Dict[str, Any]:
    return {"asset": asset, "minutes": minutes, "window": ENTRY_WINDOWS[minutes],
            "label": f"{ASSETS[asset]['label']} {LENGTH_LABEL[minutes]}"}


BTC_SERIES = {  # semua seri crypto (nama historis)
    "btc": _series("btc", 60), "btc15": _series("btc", 15), "btc5": _series("btc", 5),
    "eth": _series("eth", 60), "eth15": _series("eth", 15),
}


def hourly_slug(start_utc: datetime, asset: str = "btc") -> str:
    et = start_utc.astimezone(ET)
    hour = et.hour % 12 or 12
    prefix = ASSETS[asset]["hourly"]
    return f"{prefix}-up-or-down-{et:%B}-{et.day}-{et.year}-{hour}{'am' if et.hour < 12 else 'pm'}-et".lower()


def series_slug(series: str, start_utc: datetime) -> str:
    info = BTC_SERIES[series]
    if info["minutes"] < 60:
        return f"{info['asset']}-updown-{info['minutes']}m-{int(start_utc.timestamp())}"
    return hourly_slug(start_utc, info["asset"])


def series_start(series: str, now: datetime) -> datetime:
    minutes = BTC_SERIES[series]["minutes"]
    base = now.replace(second=0, microsecond=0)
    return base.replace(minute=(base.minute // minutes) * minutes) if minutes < 60 else base.replace(minute=0)


def _btc_market(start: datetime, series: str = "btc") -> Optional[Dict[str, Any]]:
    from app.paper_trading.live_market_data import _cached, _http

    slug = series_slug(series, start)

    def load():
        events = json.loads(_http(f"https://gamma-api.polymarket.com/events?slug={slug}") or "[]")
        if not events:
            return None
        m = events[0]["markets"][0]
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        outcomes = json.loads(m.get("outcomes") or "[]")
        if outcomes[:2] != ["Up", "Down"] or len(tokens) < 2:
            return None
        return {"condition_id": m["conditionId"], "title": events[0]["title"], "up": tokens[0], "down": tokens[1],
                "accepting": bool(m.get("acceptingOrders")) and not m.get("closed"), "slug": slug}

    return _cached(f"btc_market:{slug}", 60, load)


def _btc_klines(symbol: str = "BTCUSDT") -> List[List[float]]:
    from app.paper_trading.live_market_data import _cached, _http

    def load():
        rows = json.loads(_http(f"{BINANCE}/api/v3/klines?symbol={symbol}&interval=1m&limit=130"))
        return [[int(r[0]), float(r[1]), float(r[4])] for r in rows]

    return _cached(f"klines:{symbol}", 5, load)


def _series_klines(series: str) -> List[List[float]]:
    return _btc_klines(ASSETS[BTC_SERIES[series]["asset"]]["symbol"])


def _window(spec: str) -> Tuple[float, float]:
    try:
        a, b = (float(x) for x in str(spec).split("-", 1))
        return a, b
    except ValueError:
        return 30.0, 57.0


def btc_model(klines: List[List[float]], start: datetime, now: datetime,
              duration_minutes: int = 60) -> Optional[Dict[str, float]]:
    """P(Up) untuk rentang yang dimulai `start`, dari candle 1 menit (baris terakhir = menit berjalan)."""
    start_ms = int(start.timestamp() * 1000)
    opening = next((k for k in klines if k[0] == start_ms), None)
    if opening is None or len(klines) < 62:
        return None
    closes = [k[2] for k in klines[:-1]][-121:]  # candle yang sudah selesai
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    sigma = statistics.pstdev(rets) if len(rets) > 30 else None
    if not sigma:
        return None
    price = klines[-1][2]
    minutes_left = max((start + timedelta(minutes=duration_minutes) - now).total_seconds() / 60, 0.25)
    change = math.log(price / opening[1])
    return {"p_up": phi(change / (sigma * math.sqrt(minutes_left))), "price": price, "open": opening[1],
            "change_pct": (price / opening[1] - 1) * 100, "minutes_left": minutes_left, "sigma": sigma}


def _btc_context(series: str, now: datetime, window: Tuple[float, float]) -> Optional[Dict[str, Any]]:
    """Market + model untuk seri BTC bila sekarang berada di jendela menit yang diminta."""
    start = series_start(series, now)
    minute = (now - start).total_seconds() / 60
    if not window[0] <= minute <= window[1]:
        return None
    market = _btc_market(start, series)
    if not market or not market["accepting"]:
        return None
    model = btc_model(_series_klines(series), start, now, BTC_SERIES[series]["minutes"])
    if model is None:
        return None
    return {"series": series, "start": start, "market": market, "model": model}


def _btc_detail(model: Dict[str, float], asset: str = "btc") -> str:
    return (f"{ASSETS[asset]['label']} {model['price']:,.1f} ({model['change_pct']:+.2f}% dari open {model['open']:,.1f}) · "
            f"sisa {model['minutes_left']:.0f} menit")


BUCKET_BY_LENGTH = {60: 5, 15: 2, 5: 1}  # ember sampel sinyal (menit) per panjang rentang
SIGNAL_BUCKET_MINUTES = {name: BUCKET_BY_LENGTH[info["minutes"]] for name, info in BTC_SERIES.items()}


def btc_tick(now: Optional[datetime] = None, series: str = "btc", shadow: bool = False) -> Optional[Dict[str, Any]]:
    """
    Taker: beli sisi dengan P_model − (VWAP ask + fee) ≥ edge minimum, sekali per market.
    Setiap evaluasi dicatat sebagai sampel sinyal (per ember waktu), termasuk yang dilewati.
    shadow=True (strategi nonaktif): sinyal tetap dicatat untuk riset, tanpa membeli.
    """
    now = now or datetime.now(timezone.utc)
    ctx = _btc_context(series, now, _window(BTC_SERIES[series]["window"]()))
    if ctx is None:
        return None
    market, model = ctx["market"], ctx["model"]
    key = f"{series}|{market['condition_id']}"
    usd = float(cfg("ORDER_USD"))
    sides = []
    for outcome, token, prob, side in (("UP", market["up"], model["p_up"], "YES"),
                                       ("DOWN", market["down"], 1 - model["p_up"], "NO")):
        book = _book_side(token, usd)
        if book:
            sides.append({"outcome": outcome, "side": side, "prob": prob, "token": token,
                          "edge": prob - (book["price"] + book["fee"]), **book})
    if not sides:
        return None
    best = max(sides, key=lambda x: x["edge"])
    minute = (now - ctx["start"]).total_seconds() / 60
    # Aturan sinyal (sama untuk paper & live); paper juga butuh strategi aktif & belum trade di market ini
    if best["price"] < cfg("BTC_MIN_PRICE"):
        rule_reason = "harga di bawah minimum (underdog)"
    elif best["price"] > cfg("MAX_PRICE"):
        rule_reason = "harga di atas maksimum"
    elif best["spread"] is not None and best["spread"] > cfg("MAX_SPREAD"):
        rule_reason = "spread terlalu lebar"
    elif best["edge"] < cfg("BTC_MIN_EDGE"):
        rule_reason = "edge di bawah minimum"
    else:
        rule_reason = None
    if shadow:
        reason = "strategi nonaktif (shadow)"
    elif already_decided(key):
        reason = "sudah trade di market ini"
    else:
        reason = rule_reason
    features = {"minute": round(minute, 2), "minutes_left": round(model["minutes_left"], 2),
                "change_pct": round(model["change_pct"], 4), "sigma_1m": model.get("sigma"),
                "btc": model["price"], "asset": BTC_SERIES[series]["asset"],
                "open": model["open"], "spread": best["spread"], "depth_shares": best["shares"],
                "slippage": best.get("slippage"),
                "other_side_edge": round(min(sides, key=lambda x: x["edge"])["edge"], 4) if len(sides) > 1 else None}
    bucket = int(minute // SIGNAL_BUCKET_MINUTES[series])
    decision = {
        "key": key, "strategy": series, "market_id": market["condition_id"], "title": market["title"],
        "label": f"{market['title']} · {best['outcome']}", "side": best["side"], "outcome": best["outcome"],
        "prob": best["prob"], "price": best["price"], "fee": best["fee"], "edge": best["edge"], "size": usd,
        "detail": _btc_detail(model, BTC_SERIES[series]["asset"]), "url": f"https://polymarket.com/event/{market['slug']}",
        "features": features,
    }
    log_signal(decision, f"{key}|{bucket}", reason, now)
    if rule_reason is None:
        # Uang asli: mengikuti pilihan seri live sendiri (bukan status strategi paper), sekali per market
        try:
            from app.paper_trading.live_trader import live_execute
            live_execute({**decision, "token": best["token"], "book_price": best.get("book_price", best["price"])}, now)
        except Exception as err:
            from app.paper_trading.live_trader import redact
            logger.error("Eksekusi live gagal: %s", redact(err))
    if reason is not None:
        return None
    execute(decision, now)
    return decision


# --- Strategi maker (limit order paper) ---------------------------------------------------
#
# Pasang limit BUY di bawah harga wajar model (P − AUTOTRADE_MAKER_MARGIN), tanpa biaya taker.
# Simulasi fill konservatif: terisi hanya jika ask terbaik turun DI BAWAH harga limit (ada penjual
# yang melewati harga kita — antrian di harga yang sama dianggap tidak terisi). Order dibatalkan bila
# edge model hilang, dan kedaluwarsa di akhir jendela. Rebate maker tidak dihitung (konservatif).

MAKER_WINDOW_BY_LENGTH = {60: lambda: settings.AUTOTRADE_MAKER_WINDOW, 15: lambda: settings.AUTOTRADE_MAKER15_WINDOW,
                          5: lambda: settings.AUTOTRADE_MAKER5_WINDOW}
MAKER_WINDOWS = {name: MAKER_WINDOW_BY_LENGTH[info["minutes"]] for name, info in BTC_SERIES.items()}


def _open_limit_orders(db=None) -> List[AutotradeLimitOrder]:
    close = db is None
    db = db or get_db_session()
    try:
        return db.query(AutotradeLimitOrder).filter_by(status="open").all()
    finally:
        if close:
            db.close()


def reserved_usd() -> float:
    return sum(float(o.size_usd or 0) for o in _open_limit_orders())


def maker_place(now: Optional[datetime] = None, series: str = "btc") -> Optional[Dict[str, Any]]:
    """Pasang satu limit order per market pada sisi dengan edge terbesar."""
    from app.paper_trading.live_market_data import fetch_order_books

    now = now or datetime.now(timezone.utc)
    ctx = _btc_context(series, now, _window(MAKER_WINDOWS[series]()))
    if ctx is None:
        return None
    market, model = ctx["market"], ctx["model"]
    key = f"maker|{series}|{market['condition_id']}"
    db = get_db_session()
    try:
        if db.query(AutotradeLimitOrder.id).filter_by(decision_key=key).first() or already_decided(key):
            return None
    finally:
        db.close()
    books = fetch_order_books([market["up"], market["down"]])
    best = None
    for outcome, token, prob, side in (("UP", market["up"], model["p_up"], "YES"),
                                       ("DOWN", market["down"], 1 - model["p_up"], "NO")):
        book = books.get(str(token))
        if not book or book.get("ask") is None:
            continue
        limit = math.floor(round((prob - cfg("MAKER_MARGIN")) * 100, 6)) / 100  # 65.9999… → 66
        limit = min(limit, round(book["ask"] - 0.01, 2))  # tetap di sisi maker (tidak menyilang ask)
        if not max(0.05, cfg("BTC_MIN_PRICE")) <= limit <= cfg("MAX_PRICE"):
            continue
        edge = round(prob - limit, 4)
        # Edge sama (keduanya = margin) → pilih sisi dengan peluang model lebih tinggi (varian lebih kecil)
        if edge >= cfg("MAKER_MIN_EDGE") and (best is None or (edge, prob) > (best["edge"], best["prob"])):
            best = {"outcome": outcome, "side": side, "token": token, "prob": prob, "limit": limit, "edge": edge}
    if best is None:
        return None
    usd = float(cfg("ORDER_USD"))
    ok, reason = risk_check(usd + reserved_usd(), now)
    if not ok:
        return None
    end = ctx["start"] + timedelta(minutes=_window(MAKER_WINDOWS[series]())[1])
    db = get_db_session()
    try:
        db.add(AutotradeLimitOrder(
            decision_key=key, strategy=f"maker_{series}", market_id=market["condition_id"], token_id=best["token"],
            side=best["side"], outcome=best["outcome"], label=f"{market['title']} · {best['outcome']}"[:255],
            limit_price=Decimal(str(best["limit"])), size_usd=Decimal(str(usd)),
            model_prob=Decimal(str(round(best["prob"], 4))), status="open", created_at=now, updated_at=now,
            expires_at=end, detail=_btc_detail(model, BTC_SERIES[series]["asset"]),
            url=f"https://polymarket.com/event/{market['slug']}"))
        db.commit()
    finally:
        db.close()
    logger.info("Maker order dipasang: %s %s @ %.2f (P %.3f)", market["title"], best["outcome"], best["limit"], best["prob"])
    return best


def maker_manage(now: Optional[datetime] = None) -> List[str]:
    """Cek order terbuka: isi (ask menembus limit), batalkan (edge hilang), atau kedaluwarsa."""
    from app.paper_trading.live_market_data import fetch_order_books

    now = now or datetime.now(timezone.utc)
    orders = _open_limit_orders()
    if not orders:
        return []
    books = fetch_order_books([o.token_id for o in orders])
    events: List[str] = []
    for o in orders:
        expires = o.expires_at if o.expires_at.tzinfo else o.expires_at.replace(tzinfo=timezone.utc)
        book = books.get(str(o.token_id)) or {}
        limit = float(o.limit_price)
        status, reason = None, None
        if book.get("ask") is not None and book["ask"] < limit - float(cfg("SLIPPAGE") or 0) - 1e-9:
            status = "filled"  # slippage: ask harus menembus limit lebih dalam (antrian & adverse selection)
        elif now >= expires:
            status, reason = "expired", "tidak terisi sampai akhir jendela"
        else:
            series = o.strategy.replace("maker_", "")
            if series in BTC_SERIES:
                klines = _series_klines(series)
                start = series_start(series, now)
                model = btc_model(klines, start, now, BTC_SERIES[series]["minutes"])
                if model is not None:
                    prob = model["p_up"] if o.outcome == "UP" else 1 - model["p_up"]
                    if prob - limit < cfg("MAKER_MIN_EDGE") / 2:
                        status, reason = "cancelled", f"edge hilang (model {prob * 100:.0f}%)"
        if status is None:
            continue
        db = get_db_session()
        try:
            row = db.get(AutotradeLimitOrder, o.id)
            row.status, row.reason, row.updated_at = status, reason, now
            db.commit()
        finally:
            db.close()
        if status == "filled":
            decision = {"key": o.decision_key, "strategy": o.strategy, "market_id": o.market_id, "title": o.label,
                        "label": o.label, "side": o.side, "outcome": o.outcome, "prob": float(o.model_prob or 0),
                        "price": limit, "fee": 0.0, "edge": float(o.model_prob or 0) - limit,
                        "size": float(o.size_usd), "detail": f"MAKER fill (limit {limit * 100:.0f}¢, tanpa fee) · {o.detail or ''}",
                        "url": o.url}
            execute(decision, now)
        events.append(f"{o.decision_key}:{status}")
    return events


# --- Strategi cuaca gabungan --------------------------------------------------------------

def bracket_range(label: str):
    """'33°C' → (33, 33, 'C'); '26°C or below' → (-inf, 26, 'C'); '60-61°F' → (60, 61, 'F')."""
    import re

    m = re.search(r"(-?\d+)(?:\s*-\s*(-?\d+))?\s*°\s*([CF])(?:\s+or\s+(below|lower|higher|above))?", label or "", re.I)
    if not m:
        return None
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    tail = (m.group(4) or "").lower()
    if tail in ("below", "lower"):
        lo = -math.inf
    elif tail in ("higher", "above"):
        hi = math.inf
    return lo, hi, m.group(3).upper()


def bracket_probability(bracket, estimate: float, sigma: float, kind: str, observed: Optional[float],
                        decimal_source: bool) -> float:
    """
    P(hasil resolusi jatuh di bracket) untuk suhu akhir ~ Normal(estimate, sigma), dipotong di angka
    yang sudah terukur (max tidak bisa turun, min tidak bisa naik).
    decimal_source=True (HKO 0.1°C): bracket X = [X, X+1); selain itu bacaan dibulatkan: [X−0.5, X+0.5].
    """
    lo, hi, _ = bracket
    a = lo if decimal_source else lo - 0.5
    b = hi + 1 if decimal_source else hi + 0.5
    lower, upper = -math.inf, math.inf
    if observed is not None:
        # Bacaan bulat (METAR): tercatat 23 berarti suhu sebenarnya sudah ≥ 22.5
        slack = 0.0 if decimal_source else 0.5
        if kind == "highest":
            lower = observed - slack
        else:
            upper = observed + slack
    a, b = max(a, lower), min(b, upper)
    if a >= b:
        return 0.0
    cdf = lambda x: 0.0 if x == -math.inf else (1.0 if x == math.inf else phi((x - estimate) / sigma))  # noqa: E731
    mass = cdf(min(upper, math.inf)) - cdf(max(lower, -math.inf))
    return (cdf(b) - cdf(a)) / mass if mass > 1e-9 else 0.0


def weather_estimate(event: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
    """Perkiraan suhu akhir + sigma + angka terukur untuk sebuah event rekomendasi."""
    obs = event.get("observation") or {}
    if not obs:
        return None
    unit = obs.get("unit") or "C"
    peak_end = datetime.fromisoformat(event["peak_start"]) + timedelta(hours=settings.TEMP_PEAK_DURATION_HOURS)
    hours = max((peak_end - now).total_seconds() / 3600, 0.0)
    sigma = settings.AUTOTRADE_WEATHER_SIGMA_C + 0.3 * hours
    if unit == "F":
        sigma *= 1.8
    observed = obs.get("value")
    if obs.get("station") == "HKO" and event["kind"] == "highest":
        from app.paper_trading.hko_alerts import hko_status
        status = hko_status(now=now)
        if not status or status.get("estimate") is None:
            return None
        return {"estimate": status["estimate"], "sigma": sigma, "observed": status["max"], "unit": "C",
                "decimal": True, "source": "HKO"}
    out = obs.get("outlook")
    if not out or out.get("value") is None:
        return None
    return {"estimate": float(out["value"]), "sigma": sigma, "observed": observed, "unit": unit,
            "decimal": obs.get("station") == "HKO", "source": out.get("source") or "observasi"}


def weather_evaluate(event: Dict[str, Any], now: datetime, phase: str = "pre") -> Optional[Dict[str, Any]]:
    """
    Evaluasi satu event cuaca → dict keputusan dengan "skip_reason" (None = layak dibeli), atau None
    jika tidak ada perkiraan/bracket sama sekali. Dipakai untuk trade dan untuk sampel riset.
    """
    from app.paper_trading.recommendation_alerts import city_hashtag

    strategy = "weather" if phase == "pre" else "weather_post"
    key = f"{strategy}|{event['event_key']}"
    est = weather_estimate(event, now)
    if est is None:
        return None
    scored = []
    for m in event["markets"]:
        b = bracket_range(m.get("bracket") or "")
        if b is None:
            continue
        scored.append((bracket_probability(b, est["estimate"], est["sigma"], event["kind"], est["observed"],
                                           est["decimal"]), m))
    if not scored:
        return None
    prob, target = max(scored, key=lambda x: x[0])
    favorite = event["markets"][0]  # bracket likuid dengan peluang pasar tertinggi (apply_liquidity)
    agree = target.get("market_id") == favorite.get("market_id")
    usd = float(cfg("ORDER_USD"))
    book = _book_side(target["yes_token_id"], usd) if target.get("yes_token_id") else None
    price = book["price"] if book else (target.get("ask") or target.get("price_yes") or 0.0)
    fee = book["fee"] if book else 0.0
    edge = prob - (price + fee)
    peak_end = datetime.fromisoformat(event["peak_start"]) + timedelta(hours=settings.TEMP_PEAK_DURATION_HOURS)

    if already_decided(key):
        reason = "sudah trade di event ini"
    elif event.get("liquid") is False or target.get("liquid") is False or not book:
        reason = "tidak likuid"
    elif cfg("WEATHER_REQUIRE_AGREEMENT") and not agree:
        reason = "beda dengan favorit pasar"
    elif price > cfg("MAX_PRICE"):
        reason = "harga di atas maksimum"
    elif book["spread"] is not None and book["spread"] > cfg("MAX_SPREAD"):
        reason = "spread terlalu lebar"
    elif edge < cfg("WEATHER_MIN_EDGE"):
        reason = "edge di bawah minimum"
    else:
        reason = None
    kind = "max" if event["kind"] == "highest" else "min"
    return {
        "key": key, "strategy": strategy, "market_id": target["market_id"],
        "title": f"{city_hashtag(event['city'])} {kind} {event['local_date']} · {target['bracket']}",
        "label": f"{event['city']} {event['kind']} {event['local_date']} · {target['bracket']}",
        "side": "YES", "outcome": "YES", "prob": prob, "price": price, "fee": fee, "edge": edge, "size": usd,
        "detail": (f"Perkiraan {kind} {est['estimate']:.1f}°{est['unit']} ±{est['sigma']:.1f} ({est['source']})"
                   + (f" · terukur {est['observed']:g}°{est['unit']}" if est.get("observed") is not None else "")
                   + (" · sepakat dengan favorit pasar" if agree else " · beda dengan favorit pasar")),
        "url": target.get("polymarket_url"),
        "skip_reason": reason,
        "features": {
            "city": event["city"], "kind": event["kind"], "phase": phase, "estimate": est["estimate"],
            "sigma": round(est["sigma"], 3), "observed": est.get("observed"), "unit": est["unit"],
            "source": est["source"], "agree": agree, "favorite": favorite.get("bracket"),
            "favorite_price": favorite.get("ask") or favorite.get("price_yes"), "bracket": target.get("bracket"),
            "hours_to_peak_end": round((peak_end - now).total_seconds() / 3600, 2),
            "spread": book["spread"] if book else None, "station": (event.get("observation") or {}).get("station"),
            "slippage": book.get("slippage") if book else None,
        },
    }


def weather_decision(event: Dict[str, Any], now: datetime, phase: str = "pre") -> Optional[Dict[str, Any]]:
    """Keputusan beli untuk event cuaca, atau None jika dilewati."""
    result = weather_evaluate(event, now, phase)
    return result if result and result["skip_reason"] is None else None


def weather_tick(now: Optional[datetime] = None, phase: str = "pre") -> List[Dict[str, Any]]:
    """phase 'pre' = jendela rekomendasi menjelang puncak; 'post' = jendela observasi setelah puncak."""
    from app.paper_service import get_market_suggestions
    from app.paper_trading.recommendation_alerts import top_volume_events

    now = now or datetime.now(timezone.utc)
    events = top_volume_events([e for e in get_market_suggestions(now=now, phase=phase) if e.get("markets")], now=now)
    made = []
    for event in events:
        try:
            evaluated = weather_evaluate(event, now, phase=phase)
            if evaluated:
                bucket = int(now.timestamp() // 1800)  # satu sampel per event per 30 menit
                log_signal(evaluated, f"{evaluated['key']}|{bucket}", evaluated["skip_reason"], now)
            decision = evaluated if evaluated and evaluated["skip_reason"] is None else None
        except Exception as err:
            logger.warning("Gagal mengevaluasi %s: %s", event.get("event_key"), err)
            continue
        if decision:
            execute(decision, now)
            made.append(decision)
    return made


# --- Loop, laporan, statistik -------------------------------------------------------------

def _overview_skip(ask: Optional[float], bid: Optional[float], edge: Optional[float], in_window: bool) -> Optional[str]:
    """Alasan bot taker TIDAK akan membeli sisi ini sekarang (None = memenuhi aturan)."""
    if ask is None or edge is None:
        return "data belum lengkap"
    price = min(0.99, ask + float(cfg("SLIPPAGE") or 0))
    if price < cfg("BTC_MIN_PRICE"):
        return f"di bawah harga min {cfg('BTC_MIN_PRICE') * 100:.0f}¢"
    if price > cfg("MAX_PRICE"):
        return f"di atas harga maks {cfg('MAX_PRICE') * 100:.0f}¢"
    if bid is not None and ask - bid > cfg("MAX_SPREAD"):
        return "spread terlalu lebar"
    if edge < cfg("BTC_MIN_EDGE"):
        return f"edge < {cfg('BTC_MIN_EDGE') * 100:.0f}¢"
    if not in_window:
        return "di luar jendela masuk"
    return None


def crypto_markets_overview(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """
    Market crypto Up/Down yang sedang berjalan per seri (BTC & ETH): harga aset vs open, sisa waktu,
    peluang model, ask/bid Up & Down, biaya (ask + slippage + fee), edge, jendela masuk & status strategi.
    """
    from app.paper_trading.live_market_data import fetch_fee_rate, fetch_order_books

    now = now or datetime.now(timezone.utc)
    strategies = enabled_strategies()
    slippage = float(cfg("SLIPPAGE") or 0)
    rows: List[Dict[str, Any]] = []
    for series, info in BTC_SERIES.items():
        row: Dict[str, Any] = {"series": series, "asset": info["asset"], "label": info["label"],
                               "minutes": info["minutes"], "taker_on": series in strategies,
                               "maker_on": f"maker_{series}" in strategies}
        try:
            start = series_start(series, now)
            minute = (now - start).total_seconds() / 60
            lo, hi = _window(info["window"]())
            row.update(start=start.isoformat(), minute=round(minute, 1), window=f"{lo:g}-{hi:g}",
                       in_window=lo <= minute <= hi)
            market = _btc_market(start, series)
            if not market:
                row["error"] = "market belum tersedia"
                rows.append(row)
                continue
            row.update(title=market["title"], slug=market["slug"], accepting=market["accepting"])
            model = btc_model(_series_klines(series), start, now, info["minutes"])
            books = fetch_order_books([market["up"], market["down"]])
            sides = {}
            for outcome, token, prob in (("UP", market["up"], model["p_up"] if model else None),
                                         ("DOWN", market["down"], (1 - model["p_up"]) if model else None)):
                book = books.get(str(token)) or {}
                ask = book.get("ask")
                cost = None
                if ask is not None:
                    price = min(0.99, ask + slippage)
                    cost = price + taker_fee(price, fetch_fee_rate(token))
                edge = (prob - cost) if prob is not None and cost is not None else None
                sides[outcome] = {"ask": ask, "bid": book.get("bid"), "prob": prob, "cost": cost, "edge": edge,
                                  "skip": _overview_skip(ask, book.get("bid"), edge, row["in_window"])}
            row["sides"] = sides
            if model:
                row.update(price=model["price"], open=model["open"], change_pct=model["change_pct"],
                           minutes_left=model["minutes_left"], p_up=model["p_up"])
        except Exception as err:
            logger.warning("Gagal menyusun ringkasan %s: %s", series, err)
            row["error"] = "gagal mengambil data"
        rows.append(row)
    return rows


# --- Kalender PnL ----------------------------------------------------------------------------

def _versions_for(strategy: Optional[str]) -> List[str]:
    """Versi paper trade untuk filter: nama strategi, 'btc' / 'eth' (semua seri aset itu), atau semua."""
    if not strategy:
        return list(STRATEGY_VERSIONS.values())
    if strategy in STRATEGY_VERSIONS:
        return [STRATEGY_VERSIONS[strategy]]
    if strategy in ("btc_all", "eth_all"):
        asset = strategy.split("_")[0]
        return [STRATEGY_VERSIONS[name] for name in STRATEGY_VERSIONS
                if (BTC_SERIES.get(name.replace("maker_", "")) or {}).get("asset") == asset]
    if strategy == "weather_all":
        return [v for k, v in STRATEGY_VERSIONS.items() if k.startswith("weather")]
    raise ValueError(f"Strategi tidak dikenal: {strategy}")


def _closed(start: datetime, end: datetime, strategy: Optional[str]) -> List[PaperTrade]:
    db = get_db_session()
    try:
        return (db.query(PaperTrade).filter(PaperTrade.strategy_version.in_(_versions_for(strategy)),
                                            PaperTrade.closed_at >= start.astimezone(timezone.utc),
                                            PaperTrade.closed_at < end.astimezone(timezone.utc))
                .order_by(PaperTrade.closed_at).all())
    finally:
        db.close()


def _bucket(rows: List[PaperTrade], key) -> Dict[str, Dict[str, Any]]:
    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    out: Dict[str, Dict[str, Any]] = {}
    for t in rows:
        closed = t.closed_at if t.closed_at.tzinfo else t.closed_at.replace(tzinfo=timezone.utc)
        k = key(closed.astimezone(tz))
        b = out.setdefault(k, {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0, "cost": 0.0})
        pnl = float(t.net_pnl or 0)
        b["pnl"] += pnl
        b["trades"] += 1
        b["wins"] += 1 if pnl > 0 else 0
        b["losses"] += 1 if pnl < 0 else 0
        b["cost"] += float(t.position_size or 0)
    for b in out.values():
        b["pnl"] = round(b["pnl"], 2)
        b["cost"] = round(b["cost"], 2)
        b["roi"] = b["pnl"] / b["cost"] if b["cost"] else None
    return out


def _totals(buckets: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    pnl = round(sum(b["pnl"] for b in buckets.values()), 2)
    cost = sum(b["cost"] for b in buckets.values())
    trades = sum(b["trades"] for b in buckets.values())
    wins = sum(b["wins"] for b in buckets.values())
    days = [b["pnl"] for b in buckets.values()]
    return {"pnl": pnl, "trades": trades, "wins": wins, "win_rate": wins / trades if trades else None,
            "roi": pnl / cost if cost else None, "green": sum(1 for p in days if p > 0),
            "red": sum(1 for p in days if p < 0), "best": max(days, default=None), "worst": min(days, default=None)}


def pnl_calendar(month: Optional[str] = None, strategy: Optional[str] = None,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """PnL terealisasi auto trader per hari untuk satu bulan (YYYY-MM, zona NOTIFY_TIMEZONE), berdasarkan waktu selesai."""
    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    now = now or datetime.now(timezone.utc)
    year, mon = (int(x) for x in month.split("-")) if month else (now.astimezone(tz).year, now.astimezone(tz).month)
    start = datetime(year, mon, 1, tzinfo=tz)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=tz)
    days = _bucket(_closed(start, end, strategy), lambda d: d.date().isoformat())
    return {"month": f"{year:04d}-{mon:02d}", "strategy": strategy, "timezone": settings.NOTIFY_TIMEZONE_LABEL,
            "days": days, "totals": _totals(days)}


def pnl_calendar_year(year: Optional[int] = None, strategy: Optional[str] = None,
                      now: Optional[datetime] = None) -> Dict[str, Any]:
    """PnL terealisasi auto trader per bulan untuk satu tahun."""
    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    now = now or datetime.now(timezone.utc)
    year = year or now.astimezone(tz).year
    months = _bucket(_closed(datetime(year, 1, 1, tzinfo=tz), datetime(year + 1, 1, 1, tzinfo=tz), strategy),
                     lambda d: f"{d.year:04d}-{d.month:02d}")
    return {"year": year, "strategy": strategy, "timezone": settings.NOTIFY_TIMEZONE_LABEL,
            "months": months, "totals": _totals(months)}


def closed_trades(day: Optional[str] = None, month: Optional[str] = None, strategy: Optional[str] = None,
                  limit: int = 300) -> List[Dict[str, Any]]:
    """Trade auto yang selesai pada satu tanggal / bulan (zona NOTIFY_TIMEZONE) beserta detail keputusan."""
    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    if day:
        start = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=tz)
        end = start + timedelta(days=1)
    else:
        year, mon = (int(x) for x in str(month).split("-"))
        start = datetime(year, mon, 1, tzinfo=tz)
        end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=tz)
    rows = _closed(start, end, strategy)[-limit:]
    names = {v: k for k, v in STRATEGY_VERSIONS.items()}
    db = get_db_session()
    try:
        decisions = {}
        ids = list({t.market_id for t in rows})
        if ids:
            for d in (db.query(AutotradeDecision).filter(AutotradeDecision.market_id.in_(ids),
                                                         AutotradeDecision.status == "filled")):
                decisions[(d.market_id, d.strategy)] = d
    finally:
        db.close()
    out = []
    for t in reversed(rows):
        name = names.get(t.strategy_version, t.strategy_version)
        d = decisions.get((t.market_id, name))
        try:
            features = json.loads(d.features) if d is not None and d.features else {}
        except ValueError:
            features = {}
        closed = t.closed_at if t.closed_at.tzinfo else t.closed_at.replace(tzinfo=timezone.utc)
        opened = t.opened_at if t.opened_at.tzinfo else t.opened_at.replace(tzinfo=timezone.utc)
        pnl = float(t.net_pnl or 0)
        out.append({
            "strategy": name, "market": t.market_name or (d.label if d is not None else t.market_id),
            "outcome": (d.label.rsplit("·", 1)[-1].strip() if d is not None and d.label and "·" in d.label
                        else t.side.value if hasattr(t.side, "value") else str(t.side)),
            "entry_price": float(t.entry_price), "exit_price": float(t.exit_price) if t.exit_price is not None else None,
            "size": float(t.position_size or 0), "shares": float(t.shares or 0), "pnl": round(pnl, 2),
            "result": {"WON": "MENANG", "LOST": "KALAH", "CLOSED": "DIJUAL", "CANCELLED": "BATAL"}.get(
                t.status.value if hasattr(t.status, "value") else str(t.status), "-"),
            "opened_at": opened.isoformat(), "closed_at": closed.isoformat(),
            "prob": float(d.model_prob) if d is not None and d.model_prob is not None else None,
            "edge": float(d.edge) if d is not None and d.edge is not None else None,
            "detail": _history_detail(name, features) if features else None,
        })
    return out


def run_autotrade_tick(include_weather: bool = False) -> None:
    """Dipanggil dari collector: BTC tiap AUTOTRADE_POLL_SECONDS, cuaca tiap siklus. Tidak pernah melempar exception."""
    try:
        maybe_send_hourly_report()  # juga saat bot berhenti: posisi terbuka tetap di-settle
    except Exception as err:
        logger.error("Laporan per jam auto trade gagal: %s", err, exc_info=True)
    try:
        if not is_enabled():
            return
        strategies = enabled_strategies()
        for series in BTC_SERIES:
            btc_tick(series=series, shadow=series not in strategies)  # nonaktif: catat sinyal saja
            if f"maker_{series}" in strategies:
                maker_place(series=series)
        maker_manage()  # order yang sudah terpasang tetap dikelola walau strategi dimatikan
        if include_weather:
            if "weather" in strategies:
                weather_tick(phase="pre")
            if "weather_post" in strategies:
                weather_tick(phase="post")
        maybe_send_daily_report()
    except Exception as err:
        logger.error("Auto trader gagal: %s", err, exc_info=True)


def stats_since() -> Optional[datetime]:
    """Awal periode statistik (diatur lewat /autostats reset / sejak); None = semua waktu."""
    raw = _get_state("stats_since")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def set_stats_since(since: Optional[datetime]) -> Optional[datetime]:
    """Mulai periode statistik baru (data lama tetap tersimpan); None = kembali ke semua waktu."""
    _set_state("stats_since", since.astimezone(timezone.utc).isoformat() if since else "")
    return since


def strategy_stats(days: Optional[int] = None, now: Optional[datetime] = None,
                   all_time: bool = False) -> Dict[str, Dict[str, Any]]:
    """
    Per strategi: trade, selesai, menang, win rate, PnL terealisasi, ROI, posisi terbuka. Periode: `days`
    hari terakhir bila diisi; selain itu sejak stats_since() (kecuali all_time=True).
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days) if days else (None if all_time else stats_since())
    db = get_db_session()
    try:
        out = {}
        for name, version in STRATEGY_VERSIONS.items():
            trades = db.query(PaperTrade).filter(PaperTrade.strategy_version == version)
            decisions = db.query(AutotradeDecision).filter(AutotradeDecision.strategy == name,
                                                           AutotradeDecision.status == "filled")
            if since:
                trades = trades.filter(PaperTrade.closed_at >= since)
                decisions = decisions.filter(AutotradeDecision.created_at >= since)
            closed = trades.all()
            pnl = sum(float(t.net_pnl or 0) for t in closed)
            cost = sum(float(t.position_size or 0) for t in closed)
            wins = sum(1 for t in closed if float(t.net_pnl or 0) > 0)
            open_positions = db.query(PaperPosition).filter(PaperPosition.strategy_version == version,
                                                            PaperPosition.shares > 0).count()
            out[name] = {"trades": decisions.count(), "settled": len(closed), "wins": wins, "cost": round(cost, 2),
                         "win_rate": wins / len(closed) if closed else None, "pnl": round(pnl, 2),
                         "roi": pnl / cost if cost else None, "open": open_positions}
        return out
    finally:
        db.close()


def recent_decisions(limit: int = 20) -> List[Dict[str, Any]]:
    db = get_db_session()
    try:
        rows = db.query(AutotradeDecision).order_by(AutotradeDecision.created_at.desc()).limit(limit).all()
        return [{"strategy": r.strategy, "label": r.label, "side": r.side, "status": r.status, "reason": r.reason,
                 "prob": float(r.model_prob or 0), "price": float(r.price or 0), "fee": float(r.fee or 0),
                 "edge": float(r.edge or 0), "size": float(r.size_usd or 0), "created_at": r.created_at}
                for r in rows]
    finally:
        db.close()


def _history_detail(strategy: str, features: Dict[str, Any]) -> Optional[str]:
    """Detail keputusan: teks yang tersimpan, atau disusun dari features untuk baris lama."""
    if features.get("detail"):
        return features["detail"]
    if "change_pct" in features:
        label = ASSETS.get(features.get("asset") or strategy.replace("maker_", "").rstrip("0123456789"), ASSETS["btc"])["label"]
        return (f"{label} {features.get('btc', 0):,.1f} ({features['change_pct']:+.2f}% dari open) · "
                f"menit {features.get('minute', 0):.0f} · sisa {features.get('minutes_left', 0):.0f} menit")
    if "estimate" in features:
        kind = "max" if features.get("kind") == "highest" else "min"
        return (f"Perkiraan {kind} {features['estimate']}°{features.get('unit', '')} ±{features.get('sigma')} "
                f"({features.get('source')}) · favorit pasar {features.get('favorite')}")
    return None


def trade_history(limit: int = 20, strategy: Optional[str] = None, days: Optional[int] = None,
                  now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """
    Trade auto (status filled) terbaru beserta hasilnya: WON / LOST / CLOSED (dijual) / OPEN, PnL,
    harga masuk & keluar, peluang model, edge, dan detail keputusan.
    """
    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    try:
        q = db.query(AutotradeDecision).filter(AutotradeDecision.status == "filled")
        if strategy:
            q = q.filter(AutotradeDecision.strategy == strategy)
        if days:
            q = q.filter(AutotradeDecision.created_at >= now - timedelta(days=days))
        rows = q.order_by(AutotradeDecision.created_at.desc()).limit(limit).all()
        out = []
        for d in rows:
            version = STRATEGY_VERSIONS.get(d.strategy)
            trade = (db.query(PaperTrade).filter(PaperTrade.market_id == d.market_id,
                                                 PaperTrade.strategy_version == version)
                     .order_by(PaperTrade.opened_at.desc()).first())
            position = None
            if trade is None or trade.status == PaperTradeStatus.OPEN:
                position = (db.query(PaperPosition).filter(PaperPosition.market_id == d.market_id,
                                                           PaperPosition.strategy_version == version,
                                                           PaperPosition.shares > 0).first())
            try:
                features = json.loads(d.features) if d.features else {}
            except ValueError:
                features = {}
            status = trade.status.value if trade is not None else ("OPEN" if position is not None else "UNKNOWN")
            pnl = float(trade.net_pnl) if trade is not None and trade.net_pnl is not None else None
            if status == "OPEN" and position is not None:
                pnl = float(position.unrealized_pnl or 0)
            outcome = d.label.rsplit("·", 1)[-1].strip() if d.label and "·" in d.label else d.side
            out.append({
                "created_at": d.created_at if d.created_at.tzinfo else d.created_at.replace(tzinfo=timezone.utc),
                "strategy": d.strategy, "label": d.label, "outcome": outcome,
                "side": d.side, "prob": float(d.model_prob or 0), "price": float(d.price or 0),
                "fee": float(d.fee or 0), "edge": float(d.edge or 0), "size": float(d.size_usd or 0),
                "shares": float(trade.shares) if trade is not None else (float(position.shares) if position else None),
                "status": status, "result": {"WON": "MENANG", "LOST": "KALAH", "CLOSED": "DIJUAL",
                                             "CANCELLED": "BATAL", "OPEN": "TERBUKA"}.get(status, "-"),
                "exit_price": float(trade.exit_price) if trade is not None and trade.exit_price is not None else None,
                "pnl": pnl, "closed_at": trade.closed_at if trade is not None else None,
                "detail": _history_detail(d.strategy, features),
            })
        return out
    finally:
        db.close()


def format_trade_history(limit: int = 10, strategy: Optional[str] = None) -> str:
    """/autoriwayat: daftar trade auto terbaru dengan hasil menang/kalah dan detailnya."""
    rows = trade_history(limit=limit, strategy=strategy)
    title = f"📜 *Riwayat auto trade* ({md(strategy) if strategy else 'semua strategi'}, {len(rows)} terakhir)"
    if not rows:
        return title + "\nBelum ada trade."
    icons = {"MENANG": "✅", "KALAH": "❌", "DIJUAL": "💱", "TERBUKA": "⏳", "BATAL": "↩️"}
    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    lines = [title]
    for r in rows:
        at = r["created_at"]
        pnl = f" · PnL {r['pnl']:+.2f}" if r["pnl"] is not None else ""
        exit_txt = f" → {r['exit_price'] * 100:.0f}¢" if r["exit_price"] is not None else ""
        lines += [
            "",
            f"{icons.get(r['result'], '•')} *{r['result']}*{pnl} · {md(r['strategy'])} · {at.astimezone(tz):%d %b %H:%M}",
            f"{md(r['label'])}",
            f"Beli {md(r['outcome'])} @ {r['price'] * 100:.1f}¢ + fee {r['fee'] * 100:.1f}¢{exit_txt} · ${r['size']:.2f} · "
            f"model {r['prob'] * 100:.0f}% · edge {r['edge'] * 100:+.1f}¢",
        ]
        if r["detail"]:
            lines.append(f"_{md(r['detail'])}_")
    settled = [r for r in rows if r["result"] in ("MENANG", "KALAH", "DIJUAL")]
    if settled:
        wins = sum(1 for r in settled if (r["pnl"] or 0) > 0)
        lines += ["", f"Di daftar ini: {wins}/{len(settled)} untung · PnL {sum(r['pnl'] or 0 for r in settled):+.2f}"]
    lines.append("`/autoriwayat 20` · `/autoriwayat btc` · `/autostats` ringkasan")
    return "\n".join(lines)


def status_summary(now: Optional[datetime] = None) -> Dict[str, Any]:
    return {"enabled": is_enabled(), "strategies": enabled_strategies(), "today": today_summary(now),
            "limits": {"order_usd": float(cfg("ORDER_USD")),
                       "max_daily_usd": float(cfg("MAX_DAILY_USD")),
                       "max_daily_loss": float(cfg("MAX_DAILY_LOSS")),
                       "max_open_usd": float(cfg("MAX_OPEN_USD")),
                       "max_price": cfg("MAX_PRICE"), "btc_min_edge": cfg("BTC_MIN_EDGE"),
                       "weather_min_edge": cfg("WEATHER_MIN_EDGE"),
                       "btc_window": settings.AUTOTRADE_BTC_WINDOW, "btc15_window": settings.AUTOTRADE_BTC15_WINDOW,
                       "btc5_window": settings.AUTOTRADE_BTC5_WINDOW, "slippage": float(cfg("SLIPPAGE") or 0),
                       "maker_window": settings.AUTOTRADE_MAKER_WINDOW,
                       "maker15_window": settings.AUTOTRADE_MAKER15_WINDOW,
                       "maker_margin": cfg("MAKER_MARGIN")},
            "config": get_config(),
            "stats": strategy_stats(now=now), "stats_label": stats_label(),
            "stats_since": (lambda d: d.isoformat() if d else None)(stats_since()), "recent": recent_decisions(), "history": trade_history(limit=30, now=now),
            "open_orders": [{"strategy": o.strategy, "label": o.label, "limit": float(o.limit_price),
                             "prob": float(o.model_prob or 0), "size": float(o.size_usd), "expires_at": o.expires_at}
                            for o in _open_limit_orders()]}


def md(text: Any) -> str:
    from app.paper_trading.wallet_bot import md as _md
    return _md(text)


def stats_label(days: Optional[int] = None, all_time: bool = False) -> str:
    if days:
        return f"{days} hari"
    since = None if all_time else stats_since()
    if since is None:
        return "semua waktu"
    return f"sejak {since.astimezone(ZoneInfo(settings.NOTIFY_TIMEZONE)):%d %b %H:%M} {settings.NOTIFY_TIMEZONE_LABEL}"


def format_status(days: Optional[int] = None, all_time: bool = False) -> str:
    s = status_summary()
    t, lim = s["today"], s["limits"]
    lines = [
        f"🤖 *Auto paper trader* — {'🟢 AKTIF' if s['enabled'] else '🔴 BERHENTI'} · strategi: {md(', '.join(s['strategies'])) or '-'}",
        f"Hari ini ({t['day']}): {t['trades']} trade · beli ${t['spent']:.2f}/{lim['max_daily_usd']:.0f} · "
        f"PnL terealisasi {t['realized_pnl']:+.2f} (stop di -{lim['max_daily_loss']:.0f}) · terbuka ${t['open_usd']:.2f}",
        f"Aturan: ${lim['order_usd']:.0f}/order · harga ≤{lim['max_price'] * 100:.0f}¢ · edge crypto ≥{lim['btc_min_edge'] * 100:.0f}¢ "
        f"(1 jam menit {lim['btc_window']}, 15 menit menit {lim['btc15_window']}, 5 menit menit {lim['btc5_window']}) "
        f"· slippage {lim['slippage'] * 100:.1f}¢ · maker limit P−{lim['maker_margin'] * 100:.0f}¢ "
        f"· edge cuaca ≥{lim['weather_min_edge'] * 100:.0f}¢",
    ]
    if s["open_orders"]:
        lines.append(f"Limit order maker terbuka: {len(s['open_orders'])}")
        for o in s["open_orders"][:5]:
            lines.append(f"  • {md(o['label'])} @ {o['limit'] * 100:.0f}¢ (model {o['prob'] * 100:.0f}%)")
    lines.append("")
    stats = strategy_stats(days=days, all_time=all_time)
    label = stats_label(days, all_time)
    lines.append(f"*Hasil ({label})*")
    trades = sum(st["trades"] for st in stats.values())
    settled = sum(st["settled"] for st in stats.values())
    wins = sum(st["wins"] for st in stats.values())
    pnl = sum(st["pnl"] for st in stats.values())
    cost = sum(st["cost"] for st in stats.values())
    if settled:
        roi = f" · ROI {pnl / cost * 100:+.1f}%" if cost else ""
        lines.append(f"*Total: {trades} trade · selesai {settled} · WR {wins / settled * 100:.0f}% "
                     f"({wins}/{settled}) · PnL {pnl:+.2f}{roi}*")
    else:
        lines.append(f"*Total: {trades} trade · belum ada yang selesai*")
    for name, st in stats.items():
        wr = f"{st['win_rate'] * 100:.0f}%" if st["win_rate"] is not None else "-"
        roi = f"{st['roi'] * 100:+.1f}%" if st["roi"] is not None else "-"
        lines.append(f"• {strategy_header(name)} ({md(name)}): {st['trades']} trade · selesai {st['settled']} · WR {wr} · "
                     f"PnL {st['pnl']:+.2f} · ROI {roi} · terbuka {st['open']}")
    lines += ["", "`/stopbot` hentikan · `/startbot` jalankan · `/autostats 7` hasil 7 hari · `/autostats semua` · "
                  "`/autostats reset` mulai periode baru"]
    return "\n".join(lines)


def hourly_summary(start: datetime, end: datetime) -> Dict[str, Any]:
    """Trade auto yang dibuka & selesai dalam [start, end): jumlah, win rate, PnL total & per strategi."""
    db = get_db_session()
    try:
        opened = (db.query(AutotradeDecision).filter(AutotradeDecision.status == "filled",
                                                     AutotradeDecision.created_at >= start,
                                                     AutotradeDecision.created_at < end).all())
        versions = {v: k for k, v in STRATEGY_VERSIONS.items()}
        closed = (db.query(PaperTrade).filter(PaperTrade.strategy_version.in_(list(versions)),
                                              PaperTrade.closed_at >= start, PaperTrade.closed_at < end).all())
        per: Dict[str, Dict[str, Any]] = {}
        for t in closed:
            row = per.setdefault(versions[t.strategy_version], {"settled": 0, "wins": 0, "pnl": 0.0, "cost": 0.0})
            pnl = float(t.net_pnl or 0)
            row["settled"] += 1
            row["wins"] += 1 if pnl > 0 else 0
            row["pnl"] += pnl
            row["cost"] += float(t.position_size or 0)
        return {"opened": len(opened), "opened_usd": round(sum(float(d.size_usd or 0) for d in opened), 2),
                "settled": len(closed), "wins": sum(r["wins"] for r in per.values()),
                "pnl": round(sum(r["pnl"] for r in per.values()), 2),
                "cost": round(sum(r["cost"] for r in per.values()), 2), "per_strategy": per}
    finally:
        db.close()


def format_hourly_report(start: datetime, end: datetime, summary: Dict[str, Any]) -> str:
    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    s = summary
    lines = [f"⏱ Auto trade · {start.astimezone(tz):%H:%M}–{end.astimezone(tz):%H:%M} {settings.NOTIFY_TIMEZONE_LABEL}"]
    if s["settled"]:
        roi = f" · ROI {s['pnl'] / s['cost'] * 100:+.1f}%" if s["cost"] else ""
        lines.append(f"Selesai {s['settled']} · WR {s['wins'] / s['settled'] * 100:.0f}% ({s['wins']}/{s['settled']}) · "
                     f"PnL {s['pnl']:+.2f}{roi}")
        for name, r in sorted(s["per_strategy"].items()):
            lines.append(f"• {strategy_header(name)}: {r['wins']}/{r['settled']} menang · PnL {r['pnl']:+.2f}")
    else:
        lines.append("Belum ada trade yang selesai jam ini")
    lines.append(f"Dibuka {s['opened']} trade (${s['opened_usd']:.2f})")
    t = today_summary(end)
    lines.append(f"Hari ini: {t['trades']} trade · PnL terealisasi {t['realized_pnl']:+.2f} · terbuka ${t['open_usd']:.2f}")
    return "\n".join(lines)


def maybe_send_hourly_report(now: Optional[datetime] = None) -> bool:
    """
    Tiap pergantian jam: rangkuman 1 jam terakhir (win rate & PnL trade yang selesai, trade dibuka) — hanya
    ke grup auto trade (TELEGRAM_AUTOTRADE_CHAT_ID), tidak ke chat pribadi. Dilewati bila jam itu kosong.
    """
    from app.paper_trading.telegram import send_telegram_message

    if not settings.TELEGRAM_AUTOTRADE_CHAT_ID:
        return False
    now = now or datetime.now(timezone.utc)
    end = now.replace(minute=0, second=0, microsecond=0)
    key = end.strftime("%Y-%m-%dT%H")
    if _get_state("last_hourly_report") == key:
        return False
    _set_state("last_hourly_report", key, now)
    start = end - timedelta(hours=1)
    summary = hourly_summary(start, end)
    if not summary["opened"] and not summary["settled"]:
        return False
    result = send_telegram_message(format_hourly_report(start, end, summary), chat_id=settings.TELEGRAM_AUTOTRADE_CHAT_ID)
    if not result.get("success"):
        logger.warning("Laporan per jam auto trade tidak terkirim: %s", result.get("error"))
    return bool(result.get("success"))


def maybe_send_daily_report(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(settings.NOTIFY_TIMEZONE))
    day = local.date().isoformat()
    if local.hour < settings.AUTOTRADE_REPORT_HOUR or _get_state("last_report") == day:
        return False
    _set_state("last_report", day, now)
    notify("📬 Laporan harian auto trader\n\n" + format_status(days=1).replace("*", "").replace("`", "").replace("\\", ""))
    return True
