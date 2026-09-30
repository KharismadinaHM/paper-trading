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
    AutotradeDecision, AutotradeLimitOrder, AutotradeState, PaperPosition, PaperTrade,
)

logger = get_logger("autotrader")

STRATEGY_VERSIONS = {
    "btc": "auto_btc_v1", "btc15": "auto_btc15_v1", "maker_btc": "auto_maker_btc_v1",
    "maker_btc15": "auto_maker_btc15_v1", "weather": "auto_weather_v1", "weather_post": "auto_weather_post_v1",
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
    return [s for s in (x.strip().lower() for x in str(settings.AUTOTRADE_STRATEGIES).split(",")) if s in STRATEGY_VERSIONS]


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
    if t["realized_pnl"] <= -float(settings.AUTOTRADE_MAX_DAILY_LOSS):
        return False, f"stop harian: rugi terealisasi hari ini ${-t['realized_pnl']:.2f}"
    if t["spent"] + size > float(settings.AUTOTRADE_MAX_DAILY_USD) + 1e-9:
        return False, f"batas harian ${float(settings.AUTOTRADE_MAX_DAILY_USD):.0f} tercapai"
    if t["open_usd"] + size > float(settings.AUTOTRADE_MAX_OPEN_USD) + 1e-9:
        return False, f"batas posisi terbuka ${float(settings.AUTOTRADE_MAX_OPEN_USD):.0f} tercapai"
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
            status=status, reason=reason, local_day=_local_day(now), created_at=now))
        db.commit()
    finally:
        db.close()


def notify(text: str) -> None:
    from app.paper_trading.telegram import send_telegram_message

    result = send_telegram_message(text, chat_id=settings.TELEGRAM_AUTOTRADE_CHAT_ID or None)
    if not result.get("success"):
        logger.warning("Notifikasi auto trade tidak terkirim: %s", result.get("error"))


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
                         "max_total_exposure": Decimal(str(settings.AUTOTRADE_MAX_OPEN_USD))},
            notify=False,
        )
    except ValueError as err:
        _record(decision, "rejected", str(err)[:500], now)
        logger.info("Auto trade ditolak paper engine (%s): %s", decision["key"], err)
        return None
    _record(decision, "filled", None, now)
    notify(format_decision(decision, order))
    return order


def format_decision(d: Dict[str, Any], order: Dict[str, Any]) -> str:
    side = d.get("outcome") or d["side"]
    lines = [
        f"🤖 AUTO BUY (paper) · {d['title']}",
        f"Beli {side} {float(order['shares']):,.2f} sh @ {d['price'] * 100:.1f}¢ + fee {d['fee'] * 100:.2f}¢ = ${d['size']:.2f}",
        f"Model {d['prob'] * 100:.0f}% vs biaya {(d['price'] + d['fee']) * 100:.1f}¢ → edge {d['edge'] * 100:+.1f}¢",
    ]
    if d.get("detail"):
        lines.append(d["detail"])
    t = today_summary()
    lines.append(f"Hari ini: {t['trades']} trade · ${t['spent']:.2f}/{float(settings.AUTOTRADE_MAX_DAILY_USD):.0f} · "
                 f"PnL terealisasi {t['realized_pnl']:+.2f}")
    if d.get("url"):
        lines.append(d["url"])
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
    return {"price": fill["price"], "fee": taker_fee(fill["price"], rate), "spread": spread, "shares": fill["shares"]}


# --- Strategi BTC Up/Down (1 jam & 15 menit) ------------------------------------------------
#
# Seri 1 jam  : candle 1H BTC/USDT Binance (Up jika close ≥ open), slug bitcoin-up-or-down-…-<jam>-et.
# Seri 15 menit: TWAP Chainlink BTC/USD di akhir rentang vs harga awal, slug btc-updown-15m-<unix>.
#   Diuji pada 160 market: "close Binance ≥ open Binance" cocok 93.8% dengan hasil resolusi (rata-rata
#   sepanjang rentang hanya 85.6%), jadi keduanya dimodelkan dengan harga akhir vs harga awal.

BTC_SERIES = {
    "btc": {"minutes": 60, "window": lambda: settings.AUTOTRADE_BTC_WINDOW, "label": "1 jam"},
    "btc15": {"minutes": 15, "window": lambda: settings.AUTOTRADE_BTC15_WINDOW, "label": "15 menit"},
}


def hourly_slug(start_utc: datetime) -> str:
    et = start_utc.astimezone(ET)
    hour = et.hour % 12 or 12
    return f"bitcoin-up-or-down-{et:%B}-{et.day}-{et.year}-{hour}{'am' if et.hour < 12 else 'pm'}-et".lower()


def series_slug(series: str, start_utc: datetime) -> str:
    if series == "btc15":
        return f"btc-updown-15m-{int(start_utc.timestamp())}"
    return hourly_slug(start_utc)


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


def _btc_klines() -> List[List[float]]:
    from app.paper_trading.live_market_data import _cached, _http

    def load():
        rows = json.loads(_http(f"{BINANCE}/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=130"))
        return [[int(r[0]), float(r[1]), float(r[4])] for r in rows]

    return _cached("btc_klines", 5, load)


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
            "change_pct": (price / opening[1] - 1) * 100, "minutes_left": minutes_left}


def _btc_context(series: str, now: datetime, window: Tuple[float, float]) -> Optional[Dict[str, Any]]:
    """Market + model untuk seri BTC bila sekarang berada di jendela menit yang diminta."""
    start = series_start(series, now)
    minute = (now - start).total_seconds() / 60
    if not window[0] <= minute <= window[1]:
        return None
    market = _btc_market(start, series)
    if not market or not market["accepting"]:
        return None
    model = btc_model(_btc_klines(), start, now, BTC_SERIES[series]["minutes"])
    if model is None:
        return None
    return {"series": series, "start": start, "market": market, "model": model}


def _btc_detail(model: Dict[str, float]) -> str:
    return (f"BTC {model['price']:,.1f} ({model['change_pct']:+.2f}% dari open {model['open']:,.1f}) · "
            f"sisa {model['minutes_left']:.0f} menit")


def btc_tick(now: Optional[datetime] = None, series: str = "btc") -> Optional[Dict[str, Any]]:
    """Taker: beli sisi dengan P_model − (VWAP ask + fee) ≥ edge minimum, sekali per market."""
    now = now or datetime.now(timezone.utc)
    ctx = _btc_context(series, now, _window(BTC_SERIES[series]["window"]()))
    if ctx is None:
        return None
    market, model = ctx["market"], ctx["model"]
    key = f"{series}|{market['condition_id']}"
    if already_decided(key):
        return None
    usd = float(settings.AUTOTRADE_ORDER_USD)
    best = None
    for outcome, token, prob, side in (("UP", market["up"], model["p_up"], "YES"),
                                       ("DOWN", market["down"], 1 - model["p_up"], "NO")):
        book = _book_side(token, usd)
        if not book or book["price"] > settings.AUTOTRADE_MAX_PRICE:
            continue
        if book["spread"] is not None and book["spread"] > settings.AUTOTRADE_MAX_SPREAD:
            continue
        edge = prob - (book["price"] + book["fee"])
        if best is None or edge > best["edge"]:
            best = {"outcome": outcome, "side": side, "prob": prob, "edge": edge, **book}
    if best is None or best["edge"] < settings.AUTOTRADE_BTC_MIN_EDGE:
        return None
    decision = {
        "key": key, "strategy": series, "market_id": market["condition_id"], "title": market["title"],
        "label": f"{market['title']} · {best['outcome']}", "side": best["side"], "outcome": best["outcome"],
        "prob": best["prob"], "price": best["price"], "fee": best["fee"], "edge": best["edge"], "size": usd,
        "detail": _btc_detail(model), "url": f"https://polymarket.com/event/{market['slug']}",
    }
    execute(decision, now)
    return decision


# --- Strategi maker (limit order paper) ---------------------------------------------------
#
# Pasang limit BUY di bawah harga wajar model (P − AUTOTRADE_MAKER_MARGIN), tanpa biaya taker.
# Simulasi fill konservatif: terisi hanya jika ask terbaik turun DI BAWAH harga limit (ada penjual
# yang melewati harga kita — antrian di harga yang sama dianggap tidak terisi). Order dibatalkan bila
# edge model hilang, dan kedaluwarsa di akhir jendela. Rebate maker tidak dihitung (konservatif).

MAKER_WINDOWS = {"btc": lambda: settings.AUTOTRADE_MAKER_WINDOW, "btc15": lambda: settings.AUTOTRADE_MAKER15_WINDOW}


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
        limit = math.floor(round((prob - settings.AUTOTRADE_MAKER_MARGIN) * 100, 6)) / 100  # 65.9999… → 66
        limit = min(limit, round(book["ask"] - 0.01, 2))  # tetap di sisi maker (tidak menyilang ask)
        if not 0.05 <= limit <= settings.AUTOTRADE_MAX_PRICE:
            continue
        edge = round(prob - limit, 4)
        # Edge sama (keduanya = margin) → pilih sisi dengan peluang model lebih tinggi (varian lebih kecil)
        if edge >= settings.AUTOTRADE_MAKER_MIN_EDGE and (best is None or (edge, prob) > (best["edge"], best["prob"])):
            best = {"outcome": outcome, "side": side, "token": token, "prob": prob, "limit": limit, "edge": edge}
    if best is None:
        return None
    usd = float(settings.AUTOTRADE_ORDER_USD)
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
            expires_at=end, detail=_btc_detail(model), url=f"https://polymarket.com/event/{market['slug']}"))
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
    klines = None
    for o in orders:
        expires = o.expires_at if o.expires_at.tzinfo else o.expires_at.replace(tzinfo=timezone.utc)
        book = books.get(str(o.token_id)) or {}
        limit = float(o.limit_price)
        status, reason = None, None
        if book.get("ask") is not None and book["ask"] < limit - 1e-9:
            status = "filled"
        elif now >= expires:
            status, reason = "expired", "tidak terisi sampai akhir jendela"
        else:
            series = o.strategy.replace("maker_", "")
            if series in BTC_SERIES:
                klines = klines if klines is not None else _btc_klines()
                start = series_start(series, now)
                model = btc_model(klines, start, now, BTC_SERIES[series]["minutes"])
                if model is not None:
                    prob = model["p_up"] if o.outcome == "UP" else 1 - model["p_up"]
                    if prob - limit < settings.AUTOTRADE_MAKER_MIN_EDGE / 2:
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


def weather_decision(event: Dict[str, Any], now: datetime, phase: str = "pre") -> Optional[Dict[str, Any]]:
    from app.paper_trading.recommendation_alerts import city_hashtag

    strategy = "weather" if phase == "pre" else "weather_post"
    key = f"{strategy}|{event['event_key']}"
    if already_decided(key) or event.get("liquid") is False:
        return None
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
    if settings.AUTOTRADE_WEATHER_REQUIRE_AGREEMENT and not agree:
        return None
    if target.get("liquid") is False or not target.get("yes_token_id"):
        return None
    usd = float(settings.AUTOTRADE_ORDER_USD)
    book = _book_side(target["yes_token_id"], usd)
    if not book or book["price"] > settings.AUTOTRADE_MAX_PRICE:
        return None
    if book["spread"] is not None and book["spread"] > settings.AUTOTRADE_MAX_SPREAD:
        return None
    edge = prob - (book["price"] + book["fee"])
    if edge < settings.AUTOTRADE_WEATHER_MIN_EDGE:
        return None
    kind = "max" if event["kind"] == "highest" else "min"
    return {
        "key": key, "strategy": strategy, "market_id": target["market_id"],
        "title": f"{city_hashtag(event['city'])} {kind} {event['local_date']} · {target['bracket']}",
        "label": f"{event['city']} {event['kind']} {event['local_date']} · {target['bracket']}",
        "side": "YES", "outcome": "YES", "prob": prob, "price": book["price"], "fee": book["fee"], "edge": edge,
        "size": usd,
        "detail": (f"Perkiraan {kind} {est['estimate']:.1f}°{est['unit']} ±{est['sigma']:.1f} ({est['source']})"
                   + (f" · terukur {est['observed']:g}°{est['unit']}" if est.get("observed") is not None else "")
                   + (" · sepakat dengan favorit pasar" if agree else " · beda dengan favorit pasar")),
        "url": target.get("polymarket_url"),
    }


def weather_tick(now: Optional[datetime] = None, phase: str = "pre") -> List[Dict[str, Any]]:
    """phase 'pre' = jendela rekomendasi menjelang puncak; 'post' = jendela observasi setelah puncak."""
    from app.paper_service import get_market_suggestions
    from app.paper_trading.recommendation_alerts import top_volume_events

    now = now or datetime.now(timezone.utc)
    events = top_volume_events([e for e in get_market_suggestions(now=now, phase=phase) if e.get("markets")], now=now)
    made = []
    for event in events:
        try:
            decision = weather_decision(event, now, phase=phase)
        except Exception as err:
            logger.warning("Gagal mengevaluasi %s: %s", event.get("event_key"), err)
            continue
        if decision:
            execute(decision, now)
            made.append(decision)
    return made


# --- Loop, laporan, statistik -------------------------------------------------------------

def run_autotrade_tick(include_weather: bool = False) -> None:
    """Dipanggil dari collector: BTC tiap AUTOTRADE_POLL_SECONDS, cuaca tiap siklus. Tidak pernah melempar exception."""
    try:
        if not is_enabled():
            return
        strategies = enabled_strategies()
        for series in BTC_SERIES:
            if series in strategies:
                btc_tick(series=series)
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


def strategy_stats(days: Optional[int] = None, now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    """Per strategi: trade, selesai, menang, win rate, PnL terealisasi, ROI, posisi terbuka."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days) if days else None
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
            out[name] = {"trades": decisions.count(), "settled": len(closed), "wins": wins,
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


def status_summary(now: Optional[datetime] = None) -> Dict[str, Any]:
    return {"enabled": is_enabled(), "strategies": enabled_strategies(), "today": today_summary(now),
            "limits": {"order_usd": float(settings.AUTOTRADE_ORDER_USD),
                       "max_daily_usd": float(settings.AUTOTRADE_MAX_DAILY_USD),
                       "max_daily_loss": float(settings.AUTOTRADE_MAX_DAILY_LOSS),
                       "max_open_usd": float(settings.AUTOTRADE_MAX_OPEN_USD),
                       "max_price": settings.AUTOTRADE_MAX_PRICE, "btc_min_edge": settings.AUTOTRADE_BTC_MIN_EDGE,
                       "weather_min_edge": settings.AUTOTRADE_WEATHER_MIN_EDGE,
                       "btc_window": settings.AUTOTRADE_BTC_WINDOW, "btc15_window": settings.AUTOTRADE_BTC15_WINDOW,
                       "maker_window": settings.AUTOTRADE_MAKER_WINDOW,
                       "maker15_window": settings.AUTOTRADE_MAKER15_WINDOW,
                       "maker_margin": settings.AUTOTRADE_MAKER_MARGIN},
            "stats": strategy_stats(now=now), "recent": recent_decisions(),
            "open_orders": [{"strategy": o.strategy, "label": o.label, "limit": float(o.limit_price),
                             "prob": float(o.model_prob or 0), "size": float(o.size_usd), "expires_at": o.expires_at}
                            for o in _open_limit_orders()]}


def format_status(days: Optional[int] = None) -> str:
    s = status_summary()
    t, lim = s["today"], s["limits"]
    lines = [
        f"🤖 *Auto paper trader* — {'🟢 AKTIF' if s['enabled'] else '🔴 BERHENTI'} · strategi: {', '.join(s['strategies']) or '-'}",
        f"Hari ini ({t['day']}): {t['trades']} trade · beli ${t['spent']:.2f}/{lim['max_daily_usd']:.0f} · "
        f"PnL terealisasi {t['realized_pnl']:+.2f} (stop di -{lim['max_daily_loss']:.0f}) · terbuka ${t['open_usd']:.2f}",
        f"Aturan: ${lim['order_usd']:.0f}/order · harga ≤{lim['max_price'] * 100:.0f}¢ · edge BTC ≥{lim['btc_min_edge'] * 100:.0f}¢ "
        f"(1 jam menit {lim['btc_window']}, 15 menit menit {lim['btc15_window']}) · maker limit P−{lim['maker_margin'] * 100:.0f}¢ "
        f"· edge cuaca ≥{lim['weather_min_edge'] * 100:.0f}¢",
    ]
    if s["open_orders"]:
        lines.append(f"Limit order maker terbuka: {len(s['open_orders'])}")
        for o in s["open_orders"][:5]:
            lines.append(f"  • {o['label']} @ {o['limit'] * 100:.0f}¢ (model {o['prob'] * 100:.0f}%)")
    lines.append("")
    stats = strategy_stats(days=days)
    label = f"{days} hari" if days else "semua waktu"
    lines.append(f"*Hasil ({label})*")
    for name, st in stats.items():
        wr = f"{st['win_rate'] * 100:.0f}%" if st["win_rate"] is not None else "-"
        roi = f"{st['roi'] * 100:+.1f}%" if st["roi"] is not None else "-"
        lines.append(f"• {name}: {st['trades']} trade · selesai {st['settled']} · WR {wr} · "
                     f"PnL {st['pnl']:+.2f} · ROI {roi} · terbuka {st['open']}")
    lines += ["", "`/stopbot` hentikan · `/startbot` jalankan · `/autostats 7` hasil 7 hari"]
    return "\n".join(lines)


def maybe_send_daily_report(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(settings.NOTIFY_TIMEZONE))
    day = local.date().isoformat()
    if local.hour < settings.AUTOTRADE_REPORT_HOUR or _get_state("last_report") == day:
        return False
    _set_state("last_report", day, now)
    notify("📬 Laporan harian auto trader\n\n" + format_status(days=1).replace("*", "").replace("`", ""))
    return True
