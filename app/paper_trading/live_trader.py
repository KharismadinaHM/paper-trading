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
CHECK_INTERVAL = timedelta(minutes=10)
_client_cache: Dict[str, Any] = {}


# --- Status & saklar ------------------------------------------------------------------------

def live_strategies() -> List[str]:
    return [s.strip().lower() for s in str(settings.LIVE_STRATEGIES or "").split(",") if s.strip()]


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
    limit = min(settings.LIVE_MAX_ORDER_USD, ABSOLUTE_MAX_ORDER_USD)
    if settings.LIVE_ORDER_USD > limit:
        problems.append(f"LIVE_ORDER_USD melebihi batas per order (${limit:g})")
    if settings.LIVE_ORDER_USD < 1:
        problems.append("LIVE_ORDER_USD minimal $1 (batas order Polymarket)")
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
    """ClobClient level 2 (private key + kredensial API turunan), di-cache per proses."""
    if "client" in _client_cache:
        return _client_cache["client"]
    from py_clob_client.client import ClobClient

    client = ClobClient(settings.POLY_CLOB_HOST, key=settings.POLY_PRIVATE_KEY, chain_id=CHAIN_ID,
                        signature_type=settings.POLY_SIGNATURE_TYPE, funder=settings.POLY_FUNDER_ADDRESS or None)
    client.set_api_creds(client.create_or_derive_api_creds())
    _client_cache["client"] = client
    return client


def usdc_balance() -> Optional[float]:
    """Saldo USDC (collateral) yang bisa dipakai trading, dalam $."""
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    data = get_client().get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    raw = (data or {}).get("balance")
    return int(raw) / 1e6 if raw is not None else None


def place_fok_buy(token_id: str, usd: float, max_price: float) -> Dict[str, Any]:
    """Market order BUY FOK senilai `usd` dengan harga terburuk `max_price`. Kembalikan respons CLOB."""
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    client = get_client()
    order = client.create_market_order(MarketOrderArgs(token_id=str(token_id), amount=round(usd, 2), side=BUY,
                                                       price=max_price, order_type=OrderType.FOK))
    return client.post_order(order, OrderType.FOK) or {}


# --- Aturan ---------------------------------------------------------------------------------

def _local_day(now: datetime) -> str:
    return now.astimezone(ZoneInfo(settings.NOTIFY_TIMEZONE)).date().isoformat()


def max_price_for(prob: float, book_price: float, fee_rate: float) -> Optional[float]:
    """
    Harga tertinggi yang boleh dibayar: tidak lebih dari ask saat sinyal + LIVE_MAX_SLIPPAGE, dan edge
    (peluang − harga − fee) tetap ≥ BTC_MIN_EDGE. Dibulatkan ke bawah ke tick 1¢. None bila tidak layak.
    """
    from app.paper_trading.autotrader import cfg, taker_fee

    price = min(math.floor(round((book_price + settings.LIVE_MAX_SLIPPAGE) * 100, 6)) / 100, 0.99)
    while price >= TICK and prob - (price + taker_fee(price, fee_rate)) < cfg("BTC_MIN_EDGE") - 1e-9:
        price = round(price - TICK, 2)
    if price < max(TICK, cfg("BTC_MIN_PRICE")) or price > cfg("MAX_PRICE"):
        return None
    return price


def live_today(now: datetime) -> Dict[str, float]:
    db = get_db_session()
    try:
        day = _local_day(now)
        spent = db.query(func.coalesce(func.sum(LiveOrder.spent), 0)).filter(
            LiveOrder.local_day == day, LiveOrder.status == "filled").scalar()
        pnl = db.query(func.coalesce(func.sum(LiveOrder.pnl), 0)).filter(
            LiveOrder.local_day == day, LiveOrder.result.isnot(None)).scalar()
        open_usd = db.query(func.coalesce(func.sum(LiveOrder.spent), 0)).filter(
            LiveOrder.status == "filled", LiveOrder.result.is_(None)).scalar()
        orders = db.query(LiveOrder).filter(LiveOrder.local_day == day, LiveOrder.status == "filled").count()
        return {"day": day, "spent": float(spent or 0), "realized_pnl": float(pnl or 0),
                "open_usd": float(open_usd or 0), "orders": orders}
    finally:
        db.close()


def risk_check(usd: float, now: datetime) -> Tuple[bool, Optional[str]]:
    t = live_today(now)
    if t["realized_pnl"] <= -settings.LIVE_MAX_DAILY_LOSS:
        return False, f"stop harian live: rugi ${-t['realized_pnl']:.2f}"
    if t["spent"] + usd > settings.LIVE_MAX_DAILY_USD + 1e-9:
        return False, f"batas belanja live harian ${settings.LIVE_MAX_DAILY_USD:g} tercapai"
    if t["open_usd"] + usd > settings.LIVE_MAX_OPEN_USD + 1e-9:
        return False, f"batas posisi live terbuka ${settings.LIVE_MAX_OPEN_USD:g} tercapai"
    return True, None


# --- Eksekusi -------------------------------------------------------------------------------

def _record(decision: Dict[str, Any], status: str, now: datetime, max_price: float, **fields) -> LiveOrder:
    db = get_db_session()
    try:
        row = LiveOrder(decision_key=f"live|{decision['key']}", strategy=decision["strategy"],
                        market_id=decision["market_id"], token_id=str(decision["token"]),
                        outcome=str(decision.get("outcome") or decision["side"])[:10],
                        title=str(decision.get("title") or "")[:512],
                        usd=Decimal(str(round(settings.LIVE_ORDER_USD, 2))), max_price=Decimal(str(max_price)),
                        model_prob=Decimal(str(round(decision["prob"], 4))), status=status,
                        local_day=_local_day(now), created_at=now, **fields)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def _already(decision: Dict[str, Any]) -> bool:
    db = get_db_session()
    try:
        return db.query(LiveOrder.id).filter_by(decision_key=f"live|{decision['key']}").first() is not None
    finally:
        db.close()


def notify(text: str) -> None:
    from app.paper_trading.autotrader import notify as autotrade_notify
    autotrade_notify(text)


def _notify_once(kind: str, text: str, now: datetime) -> None:
    """Pesan masalah (saldo kurang, batas tercapai, error) sekali per jenis per hari."""
    from app.paper_trading.autotrader import _get_state, _set_state
    key = f"live_note:{_local_day(now)}:{kind}"[:100]
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
    usd = float(settings.LIVE_ORDER_USD)
    header = strategy_header(decision["strategy"])
    ok, reason = risk_check(usd, now)
    if not ok:
        kind = "risk:" + re.sub(r"[\d$.,]+", "#", reason)
        _notify_once(kind, f"⏸ LIVE dilewati · {header}\nAlasan: {reason}\n(Pesan jenis ini sekali per hari.)", now)
        return None
    max_price = max_price_for(decision["prob"], float(decision.get("book_price") or decision["price"]),
                              fetch_fee_rate(decision["token"]))
    if max_price is None:
        return None
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
    try:
        resp = place_fok_buy(decision["token"], usd, max_price)
    except Exception as err:
        message = redact(err)[:500]
        logger.error("Order live gagal: %s", message)
        row = _record(decision, "error", now, max_price, error=message)
        _notify_once("order_error", f"❌ LIVE ORDER GAGAL · {header}\n{message[:300]}", now)
        return row
    filled, making, taking = _parse_fill(resp)
    if not filled:
        error = redact(resp.get("errorMsg") or resp.get("error") or resp.get("status") or "tidak terisi")[:500]
        return _record(decision, "rejected", now, max_price, order_id=resp.get("orderID"), error=error)
    avg = making / taking if taking else None
    row = _record(decision, "filled", now, max_price, order_id=resp.get("orderID"),
                  shares=Decimal(str(round(taking, 6))), spent=Decimal(str(round(making, 6))),
                  avg_price=Decimal(str(round(avg, 6))) if avg else None)
    t = live_today(now)
    notify(f"💵 LIVE BUY (uang asli) · {header}\n{decision.get('title')}\n"
           f"Beli {decision.get('outcome')} {taking:,.2f} sh @ {(avg or 0) * 100:.1f}¢ = ${making:.2f} "
           f"(batas {max_price * 100:.0f}¢)\n"
           f"Model {decision['prob'] * 100:.0f}% · {decision.get('detail') or ''}\n"
           f"Live hari ini: {t['orders']} order · ${t['spent']:.2f}/{settings.LIVE_MAX_DAILY_USD:g} · "
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
        "limits": {"order_usd": settings.LIVE_ORDER_USD, "max_daily_usd": settings.LIVE_MAX_DAILY_USD,
                   "max_daily_loss": settings.LIVE_MAX_DAILY_LOSS, "max_open_usd": settings.LIVE_MAX_OPEN_USD,
                   "max_slippage": settings.LIVE_MAX_SLIPPAGE},
        "today": live_today(now), "filled": filled,
        "totals": {"decided": len(decided), "wins": wins, "win_rate": wins / len(decided) if decided else None,
                   "pnl": round(pnl, 2), "roi": pnl / cost if cost else None},
        "orders": [{"created_at": _aware(o.created_at).isoformat(), "strategy": o.strategy, "title": o.title,
                    "outcome": o.outcome, "status": o.status, "usd": float(o.usd), "max_price": float(o.max_price),
                    "avg_price": float(o.avg_price) if o.avg_price is not None else None,
                    "shares": float(o.shares) if o.shares is not None else None,
                    "result": o.result, "pnl": float(o.pnl) if o.pnl is not None else None,
                    "error": o.error} for o in rows],
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
             f"maks terbuka ${lim['max_open_usd']:g} · slippage maks {lim['max_slippage'] * 100:.0f}¢",
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
    lines.append("`/livestop` jeda · `/livestart` lanjut")
    return "\n".join(lines)


_last_track: Dict[str, float] = {"at": 0.0}


def run_live_tracking() -> None:
    """Dipanggil dari loop collector (tiap 5 menit); tidak pernah melempar exception."""
    import time as _time

    if not settings.LIVE_TRADING or _time.monotonic() - _last_track["at"] < 300:
        return
    _last_track["at"] = _time.monotonic()
    try:
        track_results()
    except Exception as err:
        logger.error("Pelacakan hasil live gagal: %s", redact(err))
