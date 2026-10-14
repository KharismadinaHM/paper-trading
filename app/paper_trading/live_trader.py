"""
Trading UANG ASLI di Polymarket untuk sinyal auto trader crypto (default: BTC & ETH 1 jam).

Mati secara default. Aktif hanya bila LIVE_TRADING=true, POLY_PRIVATE_KEY terisi, saklar live tidak
dimatikan lewat /livestop, dan seri strategi ada di LIVE_STRATEGIES. Dipanggil dari autotrader.btc_tick
setelah sinyal lolos semua aturan paper (edge, batas harga, spread, jendela masuk).

Eksekusi: market order FOK (isi penuh atau batal) senilai LIVE_ORDER_USD dengan BATAS HARGA =
min(ask saat sinyal + LIVE_MAX_SLIPPAGE, harga tertinggi yang edge-nya masih ≥ BTC_MIN_EDGE). Bila harga
sudah lari melewati batas, order tidak terisi dan tidak ada uang yang keluar.

Pengaman: batas keras per order, belanja harian, rugi harian terealisasi, total posisi terbuka, cek saldo
USDC sebelum order, satu order per market. Hasil (WIN/LOSS & PnL) diisi setelah market resolve.
Kemenangan harus di-claim di Polymarket agar saldo USDC kembali (lihat docs/LIVE_TRADING.md).
"""
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import func

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.models import LiveOrder

logger = get_logger("live_trader")

ABSOLUTE_MAX_ORDER_USD = 100.0    # batas di kode, tidak bisa dinaikkan lewat .env
CHAIN_ID = 137                    # Polygon
TICK = 0.01
CHECK_INTERVAL = timedelta(minutes=2)   # cek ulang hasil resolve per order (market 5/15 menit cepat selesai)
_client_cache: Dict[str, Any] = {}


# --- Pengaturan yang bisa diubah dari dashboard ---------------------------------------------
#
# Default dari .env (LIVE_*); override disimpan di autotrade_state["live_config"] dan langsung berlaku.
# LIVE_TRADING, kunci & batas keras per order (LIVE_MAX_ORDER_USD, $100 di kode) hanya dari .env.
LIVE_SERIES = ("btc", "btc15", "btc5", "eth", "eth15")
LIVE_EDITABLE: Dict[str, Tuple[str, float, float, str]] = {
    "STRATEGIES": ("strategies", 0, 0, "Seri yang dieksekusi live"),
    "ORDER_USD": ("float", 1, ABSOLUTE_MAX_ORDER_USD, "Nominal per order ($, min 1)"),
    "MAX_DAILY_USD": ("float", 1, 100000, "Maks belanja live per hari ($)"),
    "MAX_DAILY_LOSS": ("float", 1, 100000, "Stop hari itu bila rugi live ≥ ($)"),
    "MAX_OPEN_USD": ("float", 1, 100000, "Maks posisi live belum resolve ($)"),
    "MAX_SLIPPAGE": ("float", 0, 0.10, "Slippage maks di atas ask (0.02 = 2¢)"),
    "MIN_PRICE": ("float", 0.01, 0.90, "Harga beli minimum live (0.30 = 30¢)"),
    "AUTO_CLAIM": ("bool", 0, 1, "Auto-claim kemenangan"),
}


def _overrides() -> Dict[str, Any]:
    import json
    from app.paper_trading.autotrader import _get_state
    try:
        return json.loads(_get_state("live_config") or "{}") or {}
    except ValueError:
        return {}


def lcfg(name: str) -> Any:
    """Nilai pengaturan live: override dashboard bila ada, selain itu LIVE_<name> dari .env."""
    overrides = _overrides()
    return overrides[name] if name in overrides else getattr(settings, f"LIVE_{name}")


def order_cap() -> float:
    return min(float(settings.LIVE_MAX_ORDER_USD), ABSOLUTE_MAX_ORDER_USD)


def _validate(name: str, value: Any) -> Any:
    kind, lo, hi, label = LIVE_EDITABLE[name]
    if kind == "bool":
        return value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "ya", "yes", "on")
    if kind == "strategies":
        items = value if isinstance(value, list) else str(value).split(",")
        items = [x.strip().lower() for x in items if str(x).strip()]
        unknown = [x for x in items if x not in LIVE_SERIES]
        if unknown:
            raise ValueError(f"Seri tidak dikenal: {', '.join(unknown)}")
        return ",".join(items)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}: harus berupa angka")
    if math.isnan(number) or not lo <= number <= hi:
        raise ValueError(f"{label}: harus antara {lo:g} dan {hi:g}")
    if name == "ORDER_USD" and number > order_cap():
        raise ValueError(f"{label}: maksimal ${order_cap():g} (LIVE_MAX_ORDER_USD di .env)")
    return number


def set_live_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Validasi lalu simpan override. Nilai None / kosong menghapus override (kembali ke .env)."""
    import json
    from app.paper_trading.autotrader import _set_state
    unknown = [k for k in updates if k not in LIVE_EDITABLE]
    if unknown:
        raise ValueError(f"Pengaturan tidak dikenal: {', '.join(unknown)}")
    values = dict(_overrides())
    for name, value in updates.items():
        if value is None or value == "":
            values.pop(name, None)
        else:
            values[name] = _validate(name, value)
    _set_state("live_config", json.dumps(values))
    return get_live_config()


def reset_live_config() -> Dict[str, Any]:
    from app.paper_trading.autotrader import _set_state
    _set_state("live_config", "{}")
    return get_live_config()


def get_live_config() -> Dict[str, Dict[str, Any]]:
    overrides = _overrides()
    out = {}
    for name, (kind, lo, hi, label) in LIVE_EDITABLE.items():
        out[name] = {"value": lcfg(name), "default": getattr(settings, f"LIVE_{name}"), "overridden": name in overrides,
                     "type": kind, "min": lo, "max": order_cap() if name == "ORDER_USD" else hi, "label": label}
    out["STRATEGIES"]["options"] = list(LIVE_SERIES)
    return out


# --- Status & saklar ------------------------------------------------------------------------

def live_strategies() -> List[str]:
    return [s.strip().lower() for s in str(lcfg("STRATEGIES") or "").split(",") if s.strip()]


def switch_on() -> bool:
    """Saklar /livestart /livestop (default menyala bila LIVE_TRADING=true)."""
    from app.paper_trading.autotrader import _get_state
    return _get_state("live_switch") != "off"


def set_switch(on: bool) -> None:
    from app.paper_trading.autotrader import _set_state
    _set_state("live_switch", "on" if on else "off")


def config_problems() -> List[str]:
    problems = []
    if not settings.LIVE_TRADING:
        problems.append("LIVE_TRADING=false")
    if not settings.POLY_PRIVATE_KEY:
        problems.append("POLY_PRIVATE_KEY belum diisi")
    if settings.POLY_SIGNATURE_TYPE in (1, 2) and not settings.POLY_FUNDER_ADDRESS:
        problems.append("POLY_FUNDER_ADDRESS wajib untuk akun email/Magic & browser wallet")
    order_usd = float(lcfg("ORDER_USD"))
    if order_usd > order_cap():
        problems.append(f"nominal per order melebihi batas (${order_cap():g})")
    if order_usd < 1:
        problems.append("nominal per order minimal $1 (batas order Polymarket)")
    return problems


def is_active() -> bool:
    return not config_problems() and switch_on()


def redact(text: Any) -> str:
    """Jangan pernah membocorkan private key / secret di log & pesan."""
    out = str(text)
    key = settings.POLY_PRIVATE_KEY
    if key:
        out = out.replace(key, "***").replace(key.removeprefix("0x"), "***")
    return re.sub(r"0x[a-fA-F0-9]{64}", "0x***", out)


# --- Klien CLOB -----------------------------------------------------------------------------

def get_client():
    """
    ClobClient level 2 (private key + kredensial API turunan), di-cache per proses. Memakai SDK CLOB V2
    (`py-clob-client-v2`): server menolak order format lama ("invalid order version").
    """
    if "client" in _client_cache:
        return _client_cache["client"]
    from py_clob_client_v2.client import ClobClient

    client = ClobClient(settings.POLY_CLOB_HOST, CHAIN_ID, key=settings.POLY_PRIVATE_KEY,
                        signature_type=settings.POLY_SIGNATURE_TYPE, funder=settings.POLY_FUNDER_ADDRESS or None)
    client.set_api_creds(client.create_or_derive_api_key())
    _client_cache["client"] = client
    return client


def usdc_balance() -> Optional[float]:
    """Saldo collateral (USD) yang bisa dipakai trading, dalam $."""
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

    data = get_client().get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    raw = (data or {}).get("balance")
    return int(raw) / 1e6 if raw is not None else None


def place_fok_buy(token_id: str, usd: float, max_price: float, balance: Optional[float] = None) -> Dict[str, Any]:
    """
    Market order BUY FOK senilai `usd` dengan harga terburuk `max_price`. create_and_post_market_order
    mengulang otomatis bila versi order berubah. (`balance` tidak dipakai lagi; dipertahankan untuk kompatibilitas.)
    """
    from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType
    from py_clob_client_v2.order_builder.constants import BUY

    args = MarketOrderArgsV2(token_id=str(token_id), amount=round(usd, 2), side=BUY, price=max_price,
                             order_type=OrderType.FOK)
    # user_usdc_balance tidak diisi: penyesuaian fee oleh SDK bisa menghasilkan nominal >2 desimal yang ditolak
    # server ("maker amount supports a max accuracy of 2 decimals"); saldo sudah dicek sebelum order.
    return get_client().create_and_post_market_order(args, order_type=OrderType.FOK) or {}


# --- Aturan ---------------------------------------------------------------------------------

def _local_day(now: datetime) -> str:
    return now.astimezone(ZoneInfo(settings.NOTIFY_TIMEZONE)).date().isoformat()


def max_price_for(prob: float, book_price: float, fee_rate: float) -> Optional[float]:
    """
    Harga tertinggi yang boleh dibayar: tidak lebih dari ask + LIVE_MAX_SLIPPAGE (dan harga maks paper), dan
    edge (peluang − harga − fee) tetap ≥ BTC_MIN_EDGE. Dibulatkan ke bawah ke tick 1¢. None bila tidak layak:
    - batas di bawah ask (VWAP) sekarang → FOK pasti tidak terisi;
    - ask di bawah harga minimum live (LIVE_MIN_PRICE) → underdog murah dilewati.
    """
    from app.paper_trading.autotrader import cfg, taker_fee

    if book_price < float(lcfg("MIN_PRICE")) - 1e-9:
        return None
    price = math.floor(round((book_price + float(lcfg("MAX_SLIPPAGE"))) * 100, 6)) / 100
    price = min(price, float(cfg("MAX_PRICE")), 0.99)  # dibatasi, bukan dibatalkan, bila ask + slippage > maks
    while price >= TICK and prob - (price + taker_fee(price, fee_rate)) < cfg("BTC_MIN_EDGE") - 1e-9:
        price = round(price - TICK, 2)
    if price < book_price - 1e-9 or price < TICK:
        return None
    return price


MARKET_END_GRACE = timedelta(minutes=2)


def _market_running(o: LiveOrder, now: datetime) -> bool:
    """Market order ini masih berjalan? Market yang sudah lewat waktunya tidak lagi memakan slot posisi terbuka
    walau hasil resolusinya belum tercatat (pencatatan hasil bisa tertinggal beberapa menit)."""
    from app.paper_trading.autotrader import BTC_SERIES, series_start
    info = BTC_SERIES.get(o.strategy)
    if not info:
        return True
    created = _aware(o.created_at)
    end = series_start(o.strategy, created) + timedelta(minutes=info["minutes"])
    return now < end + MARKET_END_GRACE


def live_today(now: datetime) -> Dict[str, float]:
    db = get_db_session()
    try:
        day = _local_day(now)
        spent = db.query(func.coalesce(func.sum(LiveOrder.spent), 0)).filter(
            LiveOrder.local_day == day, LiveOrder.status == "filled").scalar()
        pnl = db.query(func.coalesce(func.sum(LiveOrder.pnl), 0)).filter(
            LiveOrder.local_day == day, LiveOrder.result.isnot(None)).scalar()
        unresolved = db.query(LiveOrder).filter(LiveOrder.status == "filled", LiveOrder.result.is_(None)).all()
        open_usd = sum(float(o.spent or 0) for o in unresolved if _market_running(o, now))
        orders = db.query(LiveOrder).filter(LiveOrder.local_day == day, LiveOrder.status == "filled").count()
        return {"day": day, "spent": float(spent or 0), "realized_pnl": float(pnl or 0),
                "open_usd": float(open_usd), "orders": orders,
                "awaiting_result": sum(1 for o in unresolved if not _market_running(o, now))}
    finally:
        db.close()


def risk_check(usd: float, now: datetime) -> Tuple[bool, Optional[str]]:
    t = live_today(now)
    if t["realized_pnl"] <= -float(lcfg("MAX_DAILY_LOSS")):
        return False, f"stop harian live: rugi ${-t['realized_pnl']:.2f}"
    if t["spent"] + usd > float(lcfg("MAX_DAILY_USD")) + 1e-9:
        return False, f"batas belanja live harian ${float(lcfg("MAX_DAILY_USD")):g} tercapai"
    if t["open_usd"] + usd > float(lcfg("MAX_OPEN_USD")) + 1e-9:
        return False, f"batas posisi live terbuka ${float(lcfg("MAX_OPEN_USD")):g} tercapai"
    return True, None


# --- Eksekusi -------------------------------------------------------------------------------

FOK_RETRIES = 3   # percobaan maksimal per market bila FOK tidak terisi (harga lari / book tipis)


def _base_key(decision: Dict[str, Any]) -> str:
    return f"live|{decision['key']}"


def _record(decision: Dict[str, Any], status: str, now: datetime, max_price: float, key: Optional[str] = None,
            **fields) -> LiveOrder:
    db = get_db_session()
    try:
        row = LiveOrder(decision_key=(key or _base_key(decision))[:255], strategy=decision["strategy"],
                        market_id=decision["market_id"], token_id=str(decision["token"]),
                        outcome=str(decision.get("outcome") or decision["side"])[:10],
                        title=str(decision.get("title") or "")[:512],
                        usd=Decimal(str(round(float(lcfg("ORDER_USD")), 2))), max_price=Decimal(str(max_price)),
                        model_prob=Decimal(str(round(decision["prob"], 4))), status=status,
                        local_day=_local_day(now), created_at=now, **fields)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def _rejected_attempts(decision: Dict[str, Any]) -> int:
    db = get_db_session()
    try:
        return db.query(LiveOrder).filter(LiveOrder.decision_key.like(_base_key(decision) + "|r%")).count()
    finally:
        db.close()


def _already(decision: Dict[str, Any]) -> bool:
    """Market ini sudah selesai untuk live: sudah terisi / error / dry run, atau FOK gagal FOK_RETRIES kali."""
    db = get_db_session()
    try:
        if db.query(LiveOrder.id).filter_by(decision_key=_base_key(decision)).first() is not None:
            return True
    finally:
        db.close()
    return _rejected_attempts(decision) >= FOK_RETRIES


def fresh_book_price(token: str, usd: float) -> Optional[float]:
    """Harga VWAP ask untuk `usd` dari order book SEGAR (tanpa cache 60 detik), tepat sebelum order dikirim."""
    import json as _json
    from app.paper_trading.live_market_data import _http, _summarize_book, vwap_for_usd

    try:
        book = _summarize_book(_json.loads(_http(f"{settings.POLY_CLOB_HOST}/book?token_id={token}", timeout=5)))
    except Exception as err:
        logger.warning("Order book segar gagal diambil: %s", err)
        return None
    fill = vwap_for_usd(book.get("asks") or [], usd)
    return fill["price"] if fill else None


def _is_fok_kill(message: str) -> bool:
    text = message.lower()
    return "fully filled" in text or ("fok" in text and "kill" in text)


def _state_key(prefix: str, raw: str) -> str:
    """Kunci autotrade_state ≤ 50 karakter (kolom key VARCHAR(50)): prefix + hash pendek."""
    import hashlib
    return f"{prefix}:{hashlib.sha1(raw.encode()).hexdigest()[:24]}"


def notify(text: str) -> None:
    from app.paper_trading.autotrader import notify as autotrade_notify
    autotrade_notify(text)


def _notify_once(kind: str, text: str, now: datetime) -> None:
    """Pesan masalah (saldo kurang, batas tercapai, error) sekali per jenis per hari."""
    from app.paper_trading.autotrader import _get_state, _set_state
    key = _state_key("live_note", f"{_local_day(now)}:{kind}")
    if _get_state(key):
        return
    _set_state(key, "1", now)
    notify(text)


def _parse_fill(resp: Dict[str, Any]) -> Tuple[bool, float, float]:
    """(terisi?, USDC dibayar, shares diterima) dari respons post_order BUY."""
    status = str(resp.get("status") or "").lower()
    ok = bool(resp.get("success")) and status in ("matched", "mined", "confirmed", "delayed")
    try:
        making, taking = float(resp.get("makingAmount") or 0), float(resp.get("takingAmount") or 0)
    except (TypeError, ValueError):
        making = taking = 0.0
    return ok and taking > 0, making, taking


def live_execute(decision: Dict[str, Any], now: Optional[datetime] = None) -> Optional[LiveOrder]:
    """Eksekusi uang asli untuk keputusan auto trader yang sudah lolos aturan paper. None bila dilewati."""
    from app.paper_trading.autotrader import strategy_header
    from app.paper_trading.live_market_data import fetch_fee_rate

    now = now or datetime.now(timezone.utc)
    if decision.get("strategy") not in live_strategies() or not is_active() or _already(decision):
        return None
    usd = float(lcfg("ORDER_USD"))
    header = strategy_header(decision["strategy"])
    ok, reason = risk_check(usd, now)
    if not ok:
        kind = "risk:" + re.sub(r"[\d$.,]+", "#", reason)
        _notify_once(kind, f"⏸ LIVE dilewati · {header}\nAlasan: {reason}\n(Pesan jenis ini sekali per hari.)", now)
        return None
    # Batas harga dari order book segar; bila gagal diambil, pakai harga saat sinyal
    book_price = fresh_book_price(decision["token"], usd) if not settings.LIVE_DRY_RUN else None
    max_price = max_price_for(decision["prob"], float(book_price or decision.get("book_price") or decision["price"]),
                              fetch_fee_rate(decision["token"]))
    if max_price is None:
        return None  # harga sudah lari: edge tidak cukup lagi
    if settings.LIVE_DRY_RUN:
        row = _record(decision, "dry_run", now, max_price)
        notify(f"🧪 LIVE DRY RUN · {header}\n{decision.get('title')}\n"
               f"Akan beli {decision.get('outcome')} ${usd:.2f} dengan batas harga {max_price * 100:.0f}¢ "
               f"(model {decision['prob'] * 100:.0f}%)")
        return row
    try:
        balance = usdc_balance()
    except Exception as err:
        logger.error("Cek saldo live gagal: %s", redact(err))
        _notify_once("balance_error", f"⚠️ LIVE: gagal cek saldo USDC — {redact(err)[:200]}", now)
        return None
    if balance is not None and balance < usd:
        _notify_once("balance", f"⚠️ LIVE: saldo USDC ${balance:.2f} kurang dari ${usd:.2f} per order.\n"
                     "Deposit ke wallet bot atau claim kemenangan di Polymarket.", now)
        return None
    retry_key = f"{_base_key(decision)}|r{_rejected_attempts(decision) + 1}"
    try:
        resp = place_fok_buy(decision["token"], usd, max_price, balance)
    except Exception as err:
        message = redact(err)[:500]
        if _is_fok_kill(message):
            # Bukan error: tidak ada yang terisi di harga ≤ batas, tidak ada uang keluar. Dicoba lagi tick berikutnya.
            logger.info("FOK tidak terisi (%s, batas %.2f): %s", decision["key"], max_price, message[:120])
            return _record(decision, "rejected", now, max_price, key=retry_key,
                           error=f"tidak terisi di ≤ {max_price * 100:.0f}¢ (FOK dibatalkan, tidak ada dana keluar)")
        logger.error("Order live gagal: %s", message)
        row = _record(decision, "error", now, max_price, error=message)
        _notify_once("order_error", f"❌ LIVE ORDER GAGAL · {header}\n{message[:300]}", now)
        return row
    filled, making, taking = _parse_fill(resp)
    if not filled:
        error = redact(resp.get("errorMsg") or resp.get("error") or resp.get("status") or "tidak terisi")[:500]
        return _record(decision, "rejected", now, max_price, key=retry_key, order_id=resp.get("orderID"), error=error)
    avg = making / taking if taking else None
    row = _record(decision, "filled", now, max_price, order_id=resp.get("orderID"),
                  shares=Decimal(str(round(taking, 6))), spent=Decimal(str(round(making, 6))),
                  avg_price=Decimal(str(round(avg, 6))) if avg else None)
    t = live_today(now)
    notify(f"💵 LIVE BUY (uang asli) · {header}\n{decision.get('title')}\n"
           f"Beli {decision.get('outcome')} {taking:,.2f} sh @ {(avg or 0) * 100:.1f}¢ = ${making:.2f} "
           f"(batas {max_price * 100:.0f}¢)\n"
           f"Model {decision['prob'] * 100:.0f}% · {decision.get('detail') or ''}\n"
           f"Live hari ini: {t['orders']} order · ${t['spent']:.2f}/{float(lcfg("MAX_DAILY_USD")):g} · "
           f"PnL terealisasi {t['realized_pnl']:+.2f}")
    return row


# --- Hasil ----------------------------------------------------------------------------------

def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def track_results(now: Optional[datetime] = None) -> int:
    """Isi WIN/LOSS & PnL order live yang market-nya sudah resolve."""
    from app.paper_trading.insider import market_info

    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    try:
        pending = [o for o in db.query(LiveOrder).filter(LiveOrder.status == "filled", LiveOrder.result.is_(None))
                   if o.checked_at is None or now - _aware(o.checked_at) >= CHECK_INTERVAL]
        if not pending:
            return 0
        infos = market_info([o.market_id for o in pending])
        done = 0
        for o in pending:
            o.checked_at = now
            winner = (infos.get(o.market_id) or {}).get("winner")
            if winner not in ("YES", "NO", "INVALID"):
                continue
            spent, shares = float(o.spent or 0), float(o.shares or 0)
            if winner == "INVALID":
                o.result, o.pnl = "VOID", Decimal(str(round(shares * 0.5 - spent, 6)))
            else:
                won = (winner == "YES") == (o.outcome == "UP")  # token index 0 = Up
                o.result = "WIN" if won else "LOSS"
                o.pnl = Decimal(str(round((shares if won else 0.0) - spent, 6)))
            o.resolved_at = now
            done += 1
        db.commit()
        return done
    finally:
        db.close()


def live_summary(now: Optional[datetime] = None, limit: int = 20) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    try:
        rows = db.query(LiveOrder).order_by(LiveOrder.created_at.desc()).limit(limit).all()
        decided = db.query(LiveOrder).filter(LiveOrder.result.in_(("WIN", "LOSS"))).all()
        filled = db.query(LiveOrder).filter(LiveOrder.status == "filled").count()
    finally:
        db.close()
    wins = sum(1 for o in decided if o.result == "WIN")
    pnl = sum(float(o.pnl or 0) for o in decided)
    cost = sum(float(o.spent or 0) for o in decided)
    return {
        "enabled": settings.LIVE_TRADING, "switch_on": switch_on(), "active": is_active(),
        "problems": config_problems(), "dry_run": settings.LIVE_DRY_RUN, "strategies": live_strategies(),
        "limits": {"order_usd": float(lcfg("ORDER_USD")), "max_daily_usd": float(lcfg("MAX_DAILY_USD")),
                   "max_daily_loss": float(lcfg("MAX_DAILY_LOSS")), "max_open_usd": float(lcfg("MAX_OPEN_USD")),
                   "max_slippage": float(lcfg("MAX_SLIPPAGE")), "min_price": float(lcfg("MIN_PRICE"))},
        "today": live_today(now), "filled": filled, "config": get_live_config(),
        "claim": {"active": not claim_problems(), "problems": claim_problems()},
        "totals": {"decided": len(decided), "wins": wins, "win_rate": wins / len(decided) if decided else None,
                   "pnl": round(pnl, 2), "roi": pnl / cost if cost else None},
        "orders": [{"created_at": _aware(o.created_at).isoformat(), "strategy": o.strategy, "title": o.title,
                    "outcome": o.outcome, "status": o.status, "usd": float(o.usd), "max_price": float(o.max_price),
                    "avg_price": float(o.avg_price) if o.avg_price is not None else None,
                    "shares": float(o.shares) if o.shares is not None else None,
                    "result": o.result, "pnl": float(o.pnl) if o.pnl is not None else None,
                    "claimed": o.claimed_at is not None, "error": o.error} for o in rows],
    }


def format_live_status() -> str:
    from app.paper_trading.autotrader import strategy_header
    from app.paper_trading.wallet_bot import md

    s = live_summary(limit=8)
    if s["active"]:
        state = "🟢 AKTIF" + (" (DRY RUN)" if s["dry_run"] else "")
    elif s["enabled"] and not s["switch_on"]:
        state = "⏸ DIJEDA (/livestart untuk melanjutkan)"
    else:
        state = "🔴 MATI"
    lim, t = s["limits"], s["today"]
    lines = [f"💵 *Live trading (uang asli)* — {state}",
             f"Strategi: {', '.join(strategy_header(x) for x in s['strategies']) or '-'}",
             f"Aturan: ${lim['order_usd']:g}/order · maks ${lim['max_daily_usd']:g}/hari · stop rugi ${lim['max_daily_loss']:g} · "
             f"maks terbuka ${lim['max_open_usd']:g} · slippage maks {lim['max_slippage'] * 100:.0f}¢ · "
             f"harga min {lim['min_price'] * 100:.0f}¢",
             f"Hari ini: {t['orders']} order · ${t['spent']:.2f} · PnL terealisasi {t['realized_pnl']:+.2f} · "
             f"terbuka ${t['open_usd']:.2f}"]
    if s["problems"] and s["enabled"]:
        lines.append("⚠️ " + md("; ".join(s["problems"])))
    if s["active"] and not s["dry_run"]:
        try:
            bal = usdc_balance()
            lines.append(f"Saldo USDC: ${bal:.2f}" if bal is not None else "Saldo USDC: -")
        except Exception as err:
            lines.append(f"Saldo USDC: gagal dicek ({md(redact(err)[:80])})")
    tot = s["totals"]
    if tot["decided"]:
        lines.append(f"Total: {tot['wins']}/{tot['decided']} menang · PnL {tot['pnl']:+.2f}"
                     + (f" · ROI {tot['roi'] * 100:+.1f}%" if tot["roi"] is not None else ""))
    icons = {"WIN": "✅", "LOSS": "❌", "VOID": "↩️"}
    status_icons = {"rejected": "⛔", "error": "❗", "dry_run": "🧪"}
    for o in s["orders"]:
        res = icons.get(o["result"], "⏳") if o["status"] == "filled" else status_icons.get(o["status"], "•")
        price = f"@ {o['avg_price'] * 100:.1f}¢" if o["avg_price"] else f"batas {o['max_price'] * 100:.0f}¢ · {o['status']}"
        pnl = f" · {o['pnl']:+.2f}" if o["pnl"] is not None else ""
        lines.append(f"{res} {strategy_header(o['strategy'])} {md(o['outcome'])} {price}{pnl}")
    claim = s["claim"]
    lines.append("🪙 Auto-claim: aktif" if claim["active"] else f"🪙 Auto-claim: tidak aktif ({md('; '.join(claim['problems']))})")
    lines.append("`/livestop` jeda · `/livestart` lanjut · pengaturan: dashboard → Auto Bot")
    return "\n".join(lines)


_last_track: Dict[str, float] = {"at": 0.0}


def run_live_tracking() -> None:
    """Dipanggil dari loop collector (paling cepat tiap 60 detik); tidak pernah melempar exception."""
    import time as _time

    if not settings.LIVE_TRADING or _time.monotonic() - _last_track["at"] < 60:
        return
    _last_track["at"] = _time.monotonic()
    try:
        track_results()
    except Exception as err:
        logger.error("Pelacakan hasil live gagal: %s", redact(err))
    try:
        auto_claim()
    except Exception as err:
        logger.error("Auto-claim gagal: %s", redact(err))
    try:
        maybe_send_live_hourly_report()
    except Exception as err:
        logger.error("Laporan per jam live gagal: %s", redact(err))


# --- Laporan per jam (grup auto trade) ------------------------------------------------------

def live_hourly_summary(start: datetime, end: datetime) -> Dict[str, Any]:
    """Order live dalam [start, end): dibeli (terisi / tak terisi / error), selesai (WR & PnL), per seri."""
    db = get_db_session()
    try:
        s_utc, e_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        created = db.query(LiveOrder).filter(LiveOrder.created_at >= s_utc, LiveOrder.created_at < e_utc).all()
        resolved = db.query(LiveOrder).filter(LiveOrder.status == "filled", LiveOrder.result.isnot(None),
                                              LiveOrder.resolved_at >= s_utc, LiveOrder.resolved_at < e_utc).all()
    finally:
        db.close()
    filled = [o for o in created if o.status == "filled"]
    per: Dict[str, Dict[str, Any]] = {}
    for o in resolved:
        row = per.setdefault(o.strategy, {"settled": 0, "wins": 0, "pnl": 0.0})
        row["settled"] += 1
        row["wins"] += 1 if o.result == "WIN" else 0
        row["pnl"] += float(o.pnl or 0)
    cost = sum(float(o.spent or 0) for o in resolved)
    pnl = sum(float(o.pnl or 0) for o in resolved)
    return {"bought": len(filled), "bought_usd": round(sum(float(o.spent or 0) for o in filled), 2),
            "unfilled": sum(1 for o in created if o.status == "rejected"),
            "errors": sum(1 for o in created if o.status == "error"),
            "settled": len(resolved), "wins": sum(1 for o in resolved if o.result == "WIN"),
            "pnl": round(pnl, 2), "cost": round(cost, 2), "per_strategy": per,
            "claimed": sum(1 for o in resolved if o.claimed_at is not None)}


def format_live_hourly_report(start: datetime, end: datetime, s: Dict[str, Any], balance: Optional[float]) -> str:
    from app.paper_trading.autotrader import strategy_header

    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    lines = [f"💵 Live (uang asli) · {start.astimezone(tz):%H:%M}–{end.astimezone(tz):%H:%M} {settings.NOTIFY_TIMEZONE_LABEL}"]
    if s["settled"]:
        roi = f" · ROI {s['pnl'] / s['cost'] * 100:+.1f}%" if s["cost"] else ""
        lines.append(f"Selesai {s['settled']} · WR {s['wins'] / s['settled'] * 100:.0f}% ({s['wins']}/{s['settled']}) · "
                     f"PnL {s['pnl']:+.2f}{roi}")
        for name, r in sorted(s["per_strategy"].items()):
            lines.append(f"• {strategy_header(name)}: {r['wins']}/{r['settled']} menang · PnL {r['pnl']:+.2f}")
    else:
        lines.append("Belum ada order live yang selesai jam ini")
    extra = []
    if s["unfilled"]:
        extra.append(f"{s['unfilled']} tidak terisi")
    if s["errors"]:
        extra.append(f"{s['errors']} error")
    lines.append(f"Dibeli {s['bought']} order (${s['bought_usd']:.2f})" + (f" · {' · '.join(extra)}" if extra else ""))
    t = live_today(end)
    day = f"Hari ini: {t['orders']} order · ${t['spent']:.2f} · PnL terealisasi {t['realized_pnl']:+.2f}"
    if balance is not None:
        day += f" · saldo ${balance:.2f}"
    lines.append(day)
    return "\n".join(lines)


def maybe_send_live_hourly_report(now: Optional[datetime] = None) -> bool:
    """
    Tiap pergantian jam: rekap live 1 jam terakhir, hanya ke grup auto trade (TELEGRAM_AUTOTRADE_CHAT_ID).
    Dilewati bila jam itu tidak ada order dibeli / selesai / gagal.
    """
    from app.paper_trading.autotrader import _get_state, _set_state
    from app.paper_trading.telegram import send_telegram_message

    if not settings.TELEGRAM_AUTOTRADE_CHAT_ID or not settings.LIVE_TRADING:
        return False
    now = now or datetime.now(timezone.utc)
    end = now.replace(minute=0, second=0, microsecond=0)
    key = end.strftime("%Y-%m-%dT%H")
    if _get_state("last_live_hourly") == key:
        return False
    _set_state("last_live_hourly", key, now)
    start = end - timedelta(hours=1)
    summary = live_hourly_summary(start, end)
    if not (summary["bought"] or summary["settled"] or summary["unfilled"] or summary["errors"]):
        return False
    balance = None
    if not config_problems():
        try:
            balance = usdc_balance()
        except Exception as err:
            logger.warning("Saldo untuk laporan per jam live gagal: %s", redact(err))
    result = send_telegram_message(format_live_hourly_report(start, end, summary, balance),
                                   chat_id=settings.TELEGRAM_AUTOTRADE_CHAT_ID)
    if not result.get("success"):
        logger.warning("Laporan per jam live tidak terkirim: %s", result.get("error"))
    return bool(result.get("success"))


# --- Kalender PnL live ----------------------------------------------------------------------

def _resolved_between(start: datetime, end: datetime, strategy: Optional[str]) -> List[LiveOrder]:
    db = get_db_session()
    try:
        q = db.query(LiveOrder).filter(LiveOrder.status == "filled", LiveOrder.result.isnot(None),
                                       LiveOrder.resolved_at >= start.astimezone(timezone.utc),
                                       LiveOrder.resolved_at < end.astimezone(timezone.utc))
        if strategy in ("btc_all", "eth_all"):
            from app.paper_trading.autotrader import BTC_SERIES
            asset = strategy.split("_")[0]
            q = q.filter(LiveOrder.strategy.in_([n for n, i in BTC_SERIES.items() if i["asset"] == asset]))
        elif strategy:
            q = q.filter(LiveOrder.strategy == strategy)
        return q.order_by(LiveOrder.resolved_at).all()
    finally:
        db.close()


def _live_fee(o: LiveOrder) -> float:
    """Perkiraan fee taker order live: 0.07 × p × (1 − p) per share."""
    p = float(o.avg_price or 0)
    return 0.07 * p * (1 - p) * float(o.shares or 0)


def _live_buckets(rows: List[LiveOrder], key) -> Dict[str, Dict[str, Any]]:
    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    out: Dict[str, Dict[str, Any]] = {}
    for o in rows:
        k = key(_aware(o.resolved_at).astimezone(tz))
        b = out.setdefault(k, {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0, "cost": 0.0, "fee": 0.0})
        pnl = float(o.pnl or 0)
        b["fee"] += _live_fee(o)
        b["pnl"] += pnl
        b["trades"] += 1
        b["wins"] += 1 if pnl > 0 else 0
        b["losses"] += 1 if pnl < 0 else 0
        b["cost"] += float(o.spent or 0)
    for b in out.values():
        b["pnl"], b["cost"], b["fee"] = round(b["pnl"], 2), round(b["cost"], 2), round(b["fee"], 2)
        b["roi"] = b["pnl"] / b["cost"] if b["cost"] else None
    return out


def _month_range(month: str) -> Tuple[datetime, datetime]:
    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    year, mon = (int(x) for x in month.split("-"))
    return datetime(year, mon, 1, tzinfo=tz), datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=tz)


def live_calendar(month: Optional[str] = None, strategy: Optional[str] = None,
                  now: Optional[datetime] = None) -> Dict[str, Any]:
    """PnL live terealisasi per hari (tanggal resolve, WIB) untuk satu bulan."""
    from app.paper_trading.autotrader import _totals

    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(settings.NOTIFY_TIMEZONE))
    month = month or f"{local.year:04d}-{local.month:02d}"
    start, end = _month_range(month)
    days = _live_buckets(_resolved_between(start, end, strategy), lambda d: d.date().isoformat())
    return {"month": month, "strategy": strategy, "source": "live", "timezone": settings.NOTIFY_TIMEZONE_LABEL,
            "days": days, "totals": _totals(days)}


def live_calendar_year(year: Optional[int] = None, strategy: Optional[str] = None,
                       now: Optional[datetime] = None) -> Dict[str, Any]:
    from app.paper_trading.autotrader import _totals

    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    year = year or (now or datetime.now(timezone.utc)).astimezone(tz).year
    months = _live_buckets(_resolved_between(datetime(year, 1, 1, tzinfo=tz), datetime(year + 1, 1, 1, tzinfo=tz),
                                             strategy), lambda d: f"{d.year:04d}-{d.month:02d}")
    return {"year": year, "strategy": strategy, "source": "live", "timezone": settings.NOTIFY_TIMEZONE_LABEL,
            "months": months, "totals": _totals(months)}


def live_closed(day: Optional[str] = None, month: Optional[str] = None,
                strategy: Optional[str] = None) -> List[Dict[str, Any]]:
    """Order live yang resolve pada satu tanggal / bulan (WIB), terbaru dulu — format sama dengan paper."""
    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    if day:
        start = datetime.combine(datetime.fromisoformat(day).date(), datetime.min.time(), tzinfo=tz)
        end = start + timedelta(days=1)
    else:
        start, end = _month_range(str(month))
    out = []
    for o in reversed(_resolved_between(start, end, strategy)):
        out.append({"strategy": o.strategy, "market": o.title, "outcome": o.outcome,
                    "entry_price": float(o.avg_price or o.max_price), "exit_price": 1.0 if o.result == "WIN" else 0.0,
                    "size": float(o.spent or o.usd), "shares": float(o.shares or 0), "pnl": round(float(o.pnl or 0), 2),
                    "result": {"WIN": "MENANG", "LOSS": "KALAH", "VOID": "BATAL"}.get(o.result, "-"),
                    "opened_at": _aware(o.created_at).isoformat(), "closed_at": _aware(o.resolved_at).isoformat(),
                    "prob": float(o.model_prob) if o.model_prob is not None else None, "edge": None,
                    "detail": (f"💵 live · batas {float(o.max_price) * 100:.0f}¢"
                               + (" · sudah di-claim" if o.claimed_at else ""))})
    return out


# --- Auto-claim (redeem) --------------------------------------------------------------------
#
# Posisi yang menang & sudah resolve ("redeemable") di wallet bot ditukar ke USDC dengan memanggil
# ConditionalTokens.redeemPositions(collateral, 0x0, conditionId, [1, 2]) lewat Relayer Polymarket
# (gasless; PROXY untuk akun email/Magic, SAFE untuk browser wallet). Market neg-risk dilewati (jarang
# untuk BTC/ETH Up/Down) — klaim manual di Polymarket.

REDEEM_SIGNATURE = "redeemPositions(address,bytes32,bytes32,uint256[])"
CLAIM_RETRY = timedelta(minutes=30)
CLAIM_BATCH = 10


def claim_problems() -> List[str]:
    problems = []
    if not lcfg("AUTO_CLAIM"):
        problems.append("auto-claim dimatikan")
    if settings.POLY_SIGNATURE_TYPE not in (1, 2):
        problems.append("auto-claim hanya untuk akun email/Magic (tipe 1) & browser wallet (tipe 2)")
    if not (settings.POLY_BUILDER_API_KEY and settings.POLY_BUILDER_SECRET and settings.POLY_BUILDER_PASSPHRASE):
        problems.append("kredensial Builder API (POLY_BUILDER_*) belum diisi")
    if not settings.POLY_FUNDER_ADDRESS:
        problems.append("POLY_FUNDER_ADDRESS belum diisi")
    return problems


def redeem_call_data(condition_id: str) -> Tuple[str, str]:
    """(alamat ConditionalTokens, calldata redeemPositions) untuk satu market biner."""
    from eth_abi import encode
    from eth_utils import keccak, to_checksum_address
    from py_clob_client_v2.config import get_contract_config  # collateral CLOB V2 (bukan USDC.e lama)

    contracts = get_contract_config(CHAIN_ID)
    selector = keccak(text=REDEEM_SIGNATURE)[:4]
    args = encode(["address", "bytes32", "bytes32", "uint256[]"],
                  [to_checksum_address(contracts.collateral), b"\x00" * 32,
                   bytes.fromhex(condition_id.removeprefix("0x")), [1, 2]])
    return to_checksum_address(contracts.conditional_tokens), "0x" + (selector + args).hex()


def redeemable_positions() -> Dict[str, Dict[str, Any]]:
    """{conditionId: {title, value, shares}} posisi menang yang sudah bisa di-claim di wallet bot."""
    from app.paper_trading.wallets import _get

    rows = _get("/positions", user=settings.POLY_FUNDER_ADDRESS, redeemable="true", limit=500, sizeThreshold=0.01) or []
    out: Dict[str, Dict[str, Any]] = {}
    for p in rows:
        cid = p.get("conditionId")
        if not cid or not p.get("redeemable") or p.get("negativeRisk"):
            continue
        value = float(p.get("currentValue") or 0)
        if value <= 0:
            continue  # sisi kalah: tidak ada yang bisa ditukar
        item = out.setdefault(cid, {"title": p.get("title"), "value": 0.0, "shares": 0.0})
        item["value"] += value
        item["shares"] += float(p.get("size") or 0)
    return out


def relay_client():
    if "relay" in _client_cache:
        return _client_cache["relay"]
    from py_builder_relayer_client.client import RelayClient
    from py_builder_relayer_client.models import RelayerTxType
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

    creds = BuilderApiKeyCreds(key=settings.POLY_BUILDER_API_KEY, secret=settings.POLY_BUILDER_SECRET,
                               passphrase=settings.POLY_BUILDER_PASSPHRASE)
    client = RelayClient(settings.POLY_RELAYER_URL, CHAIN_ID, private_key=settings.POLY_PRIVATE_KEY,
                         builder_config=BuilderConfig(local_builder_creds=creds),
                         relay_tx_type=RelayerTxType.PROXY if settings.POLY_SIGNATURE_TYPE == 1 else RelayerTxType.SAFE)
    _client_cache["relay"] = client
    return client


def submit_redeem(condition_ids: List[str]) -> Dict[str, Optional[str]]:
    """Kirim satu transaksi relayer berisi redeem untuk beberapa market. {transaction_id, transaction_hash}."""
    from py_builder_relayer_client.models import Transaction

    txs = []
    for cid in condition_ids:
        to, data = redeem_call_data(cid)
        txs.append(Transaction(to=to, data=data, value="0"))
    resp = relay_client().execute(txs, "redeem positions")
    return {"transaction_id": getattr(resp, "transaction_id", None), "transaction_hash": getattr(resp, "transaction_hash", None)}


def auto_claim(now: Optional[datetime] = None) -> List[str]:
    """Claim semua posisi menang yang redeemable. Kembalikan conditionId yang dikirim."""
    import json
    from app.paper_trading.autotrader import _get_state, _set_state

    now = now or datetime.now(timezone.utc)
    if config_problems() or claim_problems():
        return []
    positions = redeemable_positions()
    due = []
    for cid in positions:
        try:
            last = json.loads(_get_state(_state_key("live_claim", cid)) or "{}")
        except ValueError:
            last = {}
        at = datetime.fromisoformat(last["at"]) if last.get("at") else None
        if at is None or now - at >= CLAIM_RETRY:  # belum pernah / transaksi sebelumnya belum tercermin
            due.append(cid)
    sent: List[str] = []
    for i in range(0, len(due), CLAIM_BATCH):
        batch = due[i:i + CLAIM_BATCH]
        try:
            result = submit_redeem(batch)
        except Exception as err:
            message = redact(err)[:300]
            logger.error("Redeem gagal: %s", message)
            _notify_once("claim_error", f"⚠️ AUTO CLAIM gagal — {message}\nKlaim manual di Polymarket → Portfolio → Claim.", now)
            continue
        tx = result.get("transaction_hash") or result.get("transaction_id")
        for cid in batch:
            _set_state(_state_key("live_claim", cid), json.dumps({"at": now.isoformat(), "tx": tx}), now)
        db = get_db_session()
        try:
            for o in db.query(LiveOrder).filter(LiveOrder.market_id.in_(batch), LiveOrder.status == "filled"):
                o.claim_tx, o.claimed_at = (str(tx)[:120] if tx else None), now
            db.commit()
        finally:
            db.close()
        total = sum(positions[c]["value"] for c in batch)
        lines = [f"🪙 AUTO CLAIM · {len(batch)} market · ±${total:.2f} kembali ke saldo USDC"]
        lines += [f"• {positions[c]['title']} (${positions[c]['value']:.2f})" for c in batch[:6]]
        if result.get("transaction_hash"):
            lines.append(f"https://polygonscan.com/tx/{result['transaction_hash']}")
        notify("\n".join(lines))
        sent += batch
    return sent
