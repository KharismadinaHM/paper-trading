"""
Copy trade (PAPER): ikut membeli otomatis saat wallet yang diikuti membeli, per wallet yang dipilih.
TERPISAH dari auto bot: saklar sendiri (COPY_ENABLED, /copy start|stop), batas risiko sendiri (COPY_MAX_*),
notifikasi ke chat sendiri (TELEGRAM_COPY_CHAT_ID, default chat utama), statistik & halaman /copy sendiri.

Alur: poll_followed_wallets (tiap WALLET_POLL_SECONDS) menemukan transaksi baru wallet yang diikuti →
process_wallet_trades() untuk wallet yang copy-nya aktif:
- BUY  → beli outcome yang sama di akun paper memakai harga order book TERKINI + slippage + fee, lewat batas risiko
         copy (harian, posisi terbuka, stop rugi). Sekali per wallet + market + outcome.
         Dilewati bila: transaksi wallet sudah terlalu lama, harga sudah naik > max_slippage dari harga wallet,
         harga di luar [min_price, max_price], spread terlalu lebar, order book tipis, atau batas risiko tercapai.
         Yang dilewati dicatat dan diringkas (/copy, halaman /copy); notifikasi per kejadian hanya bila COPY_NOTIFY_SKIPS.
- SELL → (bila mirror_sell) jual posisi copy kita di market + sisi yang sama.

Pengaturan per wallet disimpan di autotrade_state ("copy|<alamat>"), bisa diubah dari dashboard & Telegram (/copy).
Catatan: analisis 1.000+ market crypto Up/Down menunjukkan wallet top tidak konsisten setelah jeda 1 menit —
copy trade lebih masuk akal di market lambat (cuaca, politik, olahraga). Uji di paper dulu.
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger

logger = get_logger("copy_trader")

STRATEGY = "copy"
VERSION = "auto_copy_v1"
DEFAULTS = {"enabled": False, "usd": None, "max_slippage": 0.03, "min_price": 0.05, "max_price": 0.95,
            "mirror_sell": True, "max_delay_min": 15}
# maks telat ≥ 3 menit: wallet dicek tiap COPY_POLL_SECONDS dan data-api Polymarket butuh ±1 menit menampilkan
# transaksi — batas 1 menit membuat hampir semua copy terlewat (12 Okt: CreamCream1215).
LIMITS = {"usd": (1, 1000), "max_slippage": (0, 0.5), "min_price": (0, 0.99), "max_price": (0.01, 0.99),
          "max_delay_min": (3, 1440)}
SCALP_WINDOW_MIN = 15      # beli lalu jual aset yang sama dalam ≤ 15 menit = scalping
SCALP_WARN_RATIO = 0.5     # ≥ 50% pembelian dijual cepat → wallet tidak cocok di-copy


# --- Saklar, batas risiko, notifikasi (terpisah dari auto bot) ---------------------------

def is_active() -> bool:
    from app.paper_trading.autotrader import _get_state
    value = _get_state("copy_enabled")
    return settings.COPY_ENABLED if value is None else value == "1"


def set_active(active: bool, now: Optional[datetime] = None) -> None:
    from app.paper_trading.autotrader import _set_state
    _set_state("copy_enabled", "1" if active else "0", now)


def notify(text: str) -> Dict[str, Any]:
    """Notifikasi copy: TELEGRAM_COPY_CHAT_ID, atau chat utama (tempat alert wallet) — bukan grup auto bot."""
    from app.paper_trading.telegram import send_telegram_message

    result = send_telegram_message(text, chat_id=settings.TELEGRAM_COPY_CHAT_ID or None)
    if not result.get("success"):
        logger.warning("Notifikasi copy tidak terkirim: %s", result.get("error"))
    return result


def today(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Pembelian copy hari ini (WIB), PnL terealisasi hari ini, dan posisi copy terbuka."""
    from zoneinfo import ZoneInfo

    from sqlalchemy import func

    from app.paper_trading.autotrader import _local_day
    from app.paper_trading.models import AutotradeDecision, PaperPosition, PaperTrade

    now = now or datetime.now(timezone.utc)
    day = _local_day(now)
    day_start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(settings.NOTIFY_TIMEZONE)).astimezone(timezone.utc)
    db = get_db_session()
    try:
        base = db.query(AutotradeDecision).filter(AutotradeDecision.strategy == STRATEGY, AutotradeDecision.local_day == day)
        spent = float(base.filter(AutotradeDecision.status == "filled")
                      .with_entities(func.coalesce(func.sum(AutotradeDecision.size_usd), 0)).scalar() or 0)
        bought = base.filter(AutotradeDecision.status == "filled").count()
        skipped = base.filter(AutotradeDecision.status == "rejected").count()
        realized = float(db.query(func.coalesce(func.sum(PaperTrade.net_pnl), 0))
                         .filter(PaperTrade.strategy_version == VERSION, PaperTrade.closed_at >= day_start).scalar() or 0)
        open_usd = float(db.query(func.coalesce(func.sum(PaperPosition.position_size), 0))
                         .filter(PaperPosition.strategy_version == VERSION, PaperPosition.shares > 0).scalar() or 0)
    finally:
        db.close()
    return {"day": day, "spent": round(spent, 2), "bought": bought, "skipped": skipped,
            "realized_pnl": round(realized, 2), "open_usd": round(open_usd, 2)}


def limits() -> Dict[str, float]:
    return {"max_daily_usd": float(settings.COPY_MAX_DAILY_USD), "max_open_usd": float(settings.COPY_MAX_OPEN_USD),
            "max_daily_loss": float(settings.COPY_MAX_DAILY_LOSS)}


def risk_check(size: float, now: Optional[datetime] = None) -> Optional[str]:
    t, lim = today(now), limits()
    if t["realized_pnl"] <= -lim["max_daily_loss"]:
        return f"stop harian copy: rugi terealisasi hari ini ${-t['realized_pnl']:.2f}"
    if t["spent"] + size > lim["max_daily_usd"] + 1e-9:
        return f"batas harian copy ${lim['max_daily_usd']:g} tercapai"
    if t["open_usd"] + size > lim["max_open_usd"] + 1e-9:
        return f"batas posisi copy terbuka ${lim['max_open_usd']:g} tercapai"
    return None


def _execute(decision: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """Risk check copy → paper order di harga order book (VWAP + slippage + fee) → catat → notifikasi copy.
    Hasil: {"action": "bought", "order"} atau hasil _skip (dengan alasan)."""
    from decimal import Decimal

    from app.paper_service import create_paper_order
    from app.paper_trading.autotrader import _record

    reason = risk_check(decision["size"], now)
    if reason:
        return _skip(decision, reason, now)
    try:
        from app.market_collector.collector import sync_markets_by_condition_ids
        sync_markets_by_condition_ids([decision["market_id"]])  # market non-cuaca tidak dikumpulkan collector
    except Exception as err:
        logger.warning("Gagal menyegarkan market %s: %s", decision["market_id"], err)
    size = Decimal(str(decision["size"]))
    try:
        order = create_paper_order(
            market_id=decision["market_id"], side=decision["side"], position_size=size, strategy_version=VERSION,
            now=now, execution_price=Decimal(str(round(decision["price"] + decision["fee"], 6))),
            risk_limits={"max_position_size": size, "max_market_exposure": size,
                         "max_total_exposure": Decimal(str(settings.COPY_MAX_OPEN_USD))},
            notify=False)
    except ValueError as err:
        return _skip(decision, f"ditolak paper engine: {str(err)[:200]}", now)
    _record(decision, "filled", None, now)
    t = today(now)
    notify(f"👛 COPY BUY (paper) · {decision['features']['wallet_label']}\n{decision['title']}\n"
           f"Beli {decision['side']} {float(order['shares']):,.2f} sh @ {decision['price'] * 100:.1f}¢ + fee "
           f"{decision['fee'] * 100:.2f}¢ = ${decision['size']:.2f}\n{decision['detail']}\n"
           f"Copy hari ini: {t['bought']} beli · ${t['spent']:.2f}/{limits()['max_daily_usd']:g} · "
           f"PnL terealisasi {t['realized_pnl']:+.2f}")
    return {"action": "bought", "key": decision["key"], "order": order}


def _key(address: str) -> str:
    return f"copy|{address.lower()}"  # ≤ 50 karakter (kolom autotrade_state.key)


def get_settings(address: str) -> Dict[str, Any]:
    from app.paper_trading.autotrader import _get_state, cfg

    try:
        stored = json.loads(_get_state(_key(address)) or "{}")
    except ValueError:
        stored = {}
    out = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
    if out["usd"] is None:
        out["usd"] = float(cfg("ORDER_USD"))
    for name, (lo, hi) in LIMITS.items():  # nilai lama di luar batas baru (mis. telat 1 menit) ikut disesuaikan
        if isinstance(out.get(name), (int, float)):
            out[name] = min(max(float(out[name]), lo), hi)
    return out


def set_settings(address: str, updates: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Validasi & simpan pengaturan copy satu wallet. Mengaktifkan copy juga mengikuti (follow) wallet itu."""
    from app.paper_trading.autotrader import _set_state
    from app.paper_trading.wallets import normalize_address, set_follow

    address = normalize_address(address)
    current = get_settings(address)
    for name, value in updates.items():
        if name not in DEFAULTS:
            raise ValueError(f"Pengaturan copy tidak dikenal: {name}")
        if name in ("enabled", "mirror_sell"):
            current[name] = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on", "ya", "yes")
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name}: harus angka") from None
        lo, hi = LIMITS[name]
        if not lo <= number <= hi:
            raise ValueError(f"{name}: harus antara {lo:g} dan {hi:g}")
        current[name] = number
    if current["min_price"] >= current["max_price"]:
        raise ValueError("min_price harus lebih kecil dari max_price")
    _set_state(_key(address), json.dumps(current), now)
    if current["enabled"]:
        set_follow(address, True, now)
    return current


def copy_wallets() -> List[str]:
    """Alamat wallet yang copy-nya aktif (untuk polling cepat)."""
    from app.paper_trading.models import TrackedWallet

    db = get_db_session()
    try:
        addresses = [w.address for w in db.query(TrackedWallet).filter_by(follow=True, status="tracking").all()]
    finally:
        db.close()
    return [a for a in addresses if get_settings(a)["enabled"]]


_style_cache: Dict[str, Any] = {}


def wallet_style(address: str, refresh: bool = False) -> Dict[str, Any]:
    """
    Gaya trading dari ±200 transaksi terakhir: berapa persen pembelian yang dijual lagi dalam ≤ SCALP_WINDOW_MIN menit
    (aset yang sama) dan median lama menahan. Wallet scalper (beli 97¢ → jual 99¢ di menit yang sama) tidak bisa
    di-copy: ask sudah ia borong, dan posisinya langsung ditutup. Cache 1 jam.
    """
    import time as _time

    from app.paper_trading.wallets import _get

    hit = _style_cache.get(address)
    if hit and not refresh and _time.monotonic() - hit[0] < 3600:
        return hit[1]
    try:
        rows = _get("/activity", user=address, limit=200, type="TRADE") or []
    except Exception as err:
        logger.warning("Gaya wallet %s gagal dibaca: %s", address, err)
        return {"buys": 0, "scalp_ratio": None, "median_hold_min": None, "scalper": False}
    buys: Dict[str, List[int]] = {}
    sells: Dict[str, List[int]] = {}
    for r in rows:
        bucket = buys if str(r.get("side")).upper() == "BUY" else sells
        bucket.setdefault(str(r.get("asset")), []).append(int(r.get("timestamp") or 0))
    holds, quick, total = [], 0, 0
    for asset, times in buys.items():
        for t in times:
            total += 1
            later = [x for x in sells.get(asset, []) if x >= t]
            if later:
                hold = (min(later) - t) / 60
                holds.append(hold)
                quick += hold <= SCALP_WINDOW_MIN
    ratio = quick / total if total else None
    holds.sort()
    style = {"buys": total, "scalp_ratio": round(ratio, 2) if ratio is not None else None,
             "median_hold_min": round(holds[len(holds) // 2], 1) if holds else None,
             "scalper": bool(total >= 6 and ratio is not None and ratio >= SCALP_WARN_RATIO)}
    _style_cache[address] = (_time.monotonic(), style)
    return style


def list_copy(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Wallet yang dilacak + pengaturan copy + hasil copy per wallet."""
    from app.paper_trading.models import TrackedWallet

    db = get_db_session()
    try:
        wallets = db.query(TrackedWallet).filter(TrackedWallet.status == "tracking").all()
        rows = [{"address": w.address, "name": w.name, "follow": bool(w.follow)} for w in wallets]
    finally:
        db.close()
    stats = copy_stats()
    for r in rows:
        r["copy"] = get_settings(r["address"])
        r["stats"] = stats.get(r["address"], {"trades": 0, "settled": 0, "wins": 0, "pnl": 0.0, "open": 0})
        r["style"] = wallet_style(r["address"]) if r["follow"] else None
    return sorted(rows, key=lambda r: (not r["copy"]["enabled"], not r["follow"], (r["name"] or r["address"]).lower()))


def copy_stats() -> Dict[str, Dict[str, Any]]:
    """Per wallet: jumlah copy, selesai, menang, PnL terealisasi, posisi terbuka (strategi copy)."""
    from app.paper_trading.models import AutotradeDecision, PaperTrade, PaperTradeStatus

    db = get_db_session()
    try:
        decisions = (db.query(AutotradeDecision)
                     .filter(AutotradeDecision.strategy == STRATEGY, AutotradeDecision.status == "filled").all())
        trades = (db.query(PaperTrade).filter(PaperTrade.strategy_version == VERSION).all())
    finally:
        db.close()
    by_market: Dict[str, List[Any]] = {}
    for t in trades:
        by_market.setdefault(t.market_id, []).append(t)
    out: Dict[str, Dict[str, Any]] = {}
    for d in decisions:
        try:
            wallet = (json.loads(d.features or "{}") or {}).get("wallet")
        except ValueError:
            wallet = None
        if not wallet:
            continue
        s = out.setdefault(wallet, {"trades": 0, "settled": 0, "wins": 0, "pnl": 0.0, "open": 0})
        s["trades"] += 1
        closed = [t for t in by_market.get(d.market_id, []) if t.status != PaperTradeStatus.OPEN and t.net_pnl is not None]
        if closed:
            pnl = sum(float(t.net_pnl) for t in closed)
            s["settled"] += 1
            s["wins"] += pnl > 0
            s["pnl"] = round(s["pnl"] + pnl, 2)
        else:
            s["open"] += 1
    for s in out.values():
        s["win_rate"] = s["wins"] / s["settled"] if s["settled"] else None
    return out


def _decision_key(address: str, condition_id: str, outcome_index: int) -> str:
    raw = f"{address.lower()}|{condition_id}|{outcome_index}"
    return f"copy|{hashlib.sha1(raw.encode()).hexdigest()[:32]}"


def _group(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Gabungkan fill kecil per market + sisi + outcome (satu order wallet sering terisi berkali-kali)."""
    groups: Dict[tuple, Dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: int(r.get("timestamp") or 0)):
        try:
            oi = int(r.get("outcomeIndex"))
        except (TypeError, ValueError):
            continue
        key = (r.get("conditionId"), str(r.get("side") or "").upper(), oi)
        g = groups.setdefault(key, {"condition_id": key[0], "side": key[1], "outcome_index": oi,
                                    "outcome": r.get("outcome"), "title": r.get("title"), "token": str(r.get("asset") or ""),
                                    "event_slug": r.get("eventSlug"), "size": 0.0, "usdc": 0.0, "timestamp": 0})
        g["size"] += float(r.get("size") or 0)
        g["usdc"] += float(r.get("usdcSize") or 0)
        g["timestamp"] = max(g["timestamp"], int(r.get("timestamp") or 0))
    for g in groups.values():
        g["price"] = g["usdc"] / g["size"] if g["size"] else None
    return list(groups.values())


def _label(wallet: Any) -> str:
    from app.paper_trading.wallets import clean_name, short
    return clean_name(getattr(wallet, "name", None)) or short(wallet.address)


def _skip(decision: Dict[str, Any], reason: str, now: datetime, quiet: bool = False) -> Dict[str, Any]:
    """Catat copy yang dilewati (sekali per wallet + market + outcome); kabari hanya bila COPY_NOTIFY_SKIPS."""
    from app.paper_trading.autotrader import _record

    _record(decision, "rejected", reason[:500], now)
    if settings.COPY_NOTIFY_SKIPS and not quiet:
        notify(f"⏭ COPY DILEWATI (paper) · 👛 {decision['features']['wallet_label']}\n{decision['title']}\nAlasan: {reason}")
    return {"action": "skipped", "reason": reason, "key": decision["key"]}


SKIP_GROUPS = (("harga sudah naik", "harga sudah lari"), ("ask kosong", "ask habis diborong"),
               ("order book", "order book tipis/tidak ada"),
               ("di luar batas", "harga di luar batas"), ("spread", "spread lebar"), ("menit lalu", "terlambat"),
               ("batas", "batas risiko copy"), ("stop harian", "stop rugi harian"))


def skip_group(reason: str) -> str:
    for needle, label in SKIP_GROUPS:
        if needle in (reason or ""):
            return label
    return "lainnya"


def recent_skips(limit: int = 50, hours: int = 24, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    from datetime import timedelta

    from app.paper_trading.models import AutotradeDecision

    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    try:
        rows = (db.query(AutotradeDecision).filter(AutotradeDecision.strategy == STRATEGY,
                                                   AutotradeDecision.status == "rejected",
                                                   AutotradeDecision.created_at >= now - timedelta(hours=hours))
                .order_by(AutotradeDecision.created_at.desc()).limit(limit).all())
    finally:
        db.close()
    out = []
    for r in rows:
        try:
            f = json.loads(r.features or "{}")
        except ValueError:
            f = {}
        out.append({"created_at": (r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)).isoformat(),
                    "wallet": f.get("wallet"), "wallet_label": f.get("wallet_label"), "label": r.label,
                    "reason": r.reason, "group": skip_group(r.reason), "wallet_price": f.get("wallet_price"),
                    "price": float(r.price or 0)})
    return out


def skip_summary(hours: int = 24, now: Optional[datetime] = None) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for s in recent_skips(limit=1000, hours=hours, now=now):
        out[s["group"]] = out.get(s["group"], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _book_problem(token: str, usd: float) -> str:
    """Kenapa tidak bisa membeli: tidak ada order book, ask habis, atau terlalu tipis untuk nominal copy."""
    from app.paper_trading.live_market_data import FRESH_BOOK_TTL, fetch_order_books

    if not token:
        return "token outcome tidak diketahui"
    try:
        book = fetch_order_books([token], ttl=FRESH_BOOK_TTL).get(str(token))
    except Exception:
        book = None
    if not book:
        return "market tidak punya order book (tutup / belum dibuka)"
    if not book.get("asks"):
        return "tidak ada penjual: ask kosong (kemungkinan sudah diborong wallet)"
    return f"order book terlalu tipis untuk ${usd:g}"


def copy_buy(wallet: Any, trade: Dict[str, Any], cfg_wallet: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
    """Salin satu pembelian wallet. Mengembalikan ringkasan hasil, atau None bila sudah pernah diproses."""
    from app.paper_trading.autotrader import _book_side, already_decided, cfg

    key = _decision_key(wallet.address, trade["condition_id"], trade["outcome_index"])
    if already_decided(key):
        return None
    side = "YES" if trade["outcome_index"] == 0 else "NO"
    usd = float(cfg_wallet["usd"])
    wallet_price = trade["price"] or 0.0
    label = _label(wallet)
    book = _book_side(trade["token"], usd) if trade["token"] else None
    price = book["price"] if book else wallet_price
    fee = book["fee"] if book else 0.0
    decision = {
        "key": key, "strategy": STRATEGY, "market_id": trade["condition_id"],
        "title": f"{trade['title']} · {trade['outcome']}",
        "label": f"👛 {label} · {trade['title']} · {trade['outcome']}"[:255],
        "side": side, "outcome": trade["outcome"], "prob": wallet_price, "price": price, "fee": fee,
        "edge": wallet_price - price, "size": usd,
        "detail": (f"Salin 👛 {label}: BUY {trade['size']:,.1f} sh @ {wallet_price * 100:.1f}¢ (${trade['usdc']:,.2f}) · "
                   f"kita @ {price * 100:.1f}¢ ({(price - wallet_price) * 100:+.1f}¢)"
                   + (f" · https://polymarket.com/event/{trade['event_slug']}" if trade.get("event_slug") else "")),
        "features": {"wallet": wallet.address, "wallet_label": label, "wallet_price": wallet_price,
                     "wallet_usdc": round(trade["usdc"], 2), "delay_s": int(now.timestamp()) - trade["timestamp"],
                     "token": trade["token"], "slippage": book.get("slippage") if book else None},
    }
    age_min = (now.timestamp() - trade["timestamp"]) / 60
    if age_min > float(cfg_wallet["max_delay_min"]):
        return _skip(decision, f"transaksi wallet sudah {age_min:.0f} menit lalu (maks {cfg_wallet['max_delay_min']:g})",
                     now, quiet=True)
    if not book:
        return _skip(decision, _book_problem(trade["token"], usd), now)
    if price - wallet_price > float(cfg_wallet["max_slippage"]) + 1e-9:
        return _skip(decision, f"harga sudah naik {wallet_price * 100:.1f}¢ → {price * 100:.1f}¢ "
                               f"(maks +{float(cfg_wallet['max_slippage']) * 100:.0f}¢)", now)
    if price < float(cfg_wallet["min_price"]) or price > float(cfg_wallet["max_price"]):
        return _skip(decision, f"harga {price * 100:.1f}¢ di luar batas {float(cfg_wallet['min_price']) * 100:.0f}–"
                               f"{float(cfg_wallet['max_price']) * 100:.0f}¢", now)
    if book.get("spread") is not None and book["spread"] > float(cfg("MAX_SPREAD")):
        return _skip(decision, f"spread {book['spread'] * 100:.1f}¢ terlalu lebar", now)
    return _execute(decision, now)


def copy_sell(wallet: Any, trade: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
    """Wallet menjual: jual posisi copy kita (strategi copy) di market + sisi yang sama, bila ada."""
    from app.paper_service import sell_paper_position
    from app.paper_trading.models import PaperPosition, TradeSide

    side = "YES" if trade["outcome_index"] == 0 else "NO"
    db = get_db_session()
    try:
        pos = (db.query(PaperPosition).filter(PaperPosition.market_id == trade["condition_id"],
                                              PaperPosition.side == TradeSide(side),
                                              PaperPosition.strategy_version == VERSION,
                                              PaperPosition.shares > 0).first())
    finally:
        db.close()
    if pos is None:
        return None
    label = _label(wallet)
    try:
        result = sell_paper_position(trade["condition_id"], side, now=now)
    except ValueError as err:
        notify(f"⚠️ COPY SELL GAGAL (paper) · 👛 {label}\n{trade['title']} · {trade['outcome']}\n{err}")
        return {"action": "sell_failed", "reason": str(err)}
    pnl = result.get("realized_pnl")
    notify(f"👛 COPY SELL (paper) · {label}\n{trade['title']} · {trade['outcome']}\n"
           f"Wallet menjual {trade['size']:,.1f} sh @ {(trade['price'] or 0) * 100:.1f}¢ → posisi copy kita ikut dijual"
           + (f" · PnL {float(pnl):+.2f}" if pnl is not None else ""))
    return {"action": "sold", "result": result}


def process_wallet_trades(wallet: Any, rows: List[Dict[str, Any]], now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Dipanggil dari poll_followed_wallets dengan transaksi baru satu wallet. Tidak pernah melempar exception."""
    now = now or datetime.now(timezone.utc)
    results: List[Dict[str, Any]] = []
    try:
        cfg_wallet = get_settings(wallet.address)
        if not cfg_wallet["enabled"] or not rows or not is_active():
            return results
        for trade in _group(rows):
            try:
                if trade["side"] == "BUY":
                    r = copy_buy(wallet, trade, cfg_wallet, now)
                elif trade["side"] == "SELL" and cfg_wallet["mirror_sell"]:
                    r = copy_sell(wallet, trade, now)
                else:
                    r = None
            except Exception as err:
                logger.warning("Copy trade %s gagal: %s", wallet.address, err, exc_info=True)
                r = {"action": "error", "reason": str(err)}
            if r:
                results.append(r)
    except Exception as err:
        logger.error("Copy trade wallet %s gagal: %s", wallet.address, err, exc_info=True)
    return results


def format_copy_list() -> str:
    """/copy: status copy trade (terpisah dari auto bot) per wallet, batas, hari ini, dan yang dilewati."""
    from app.paper_trading.wallet_bot import md

    rows = list_copy()
    t, lim = today(), limits()
    lines = ["👛 *Copy trade (paper)* — " + ("🟢 aktif" if is_active() else "🔴 berhenti (`/copy start`)"),
             f"Hari ini: {t['bought']} beli · ${t['spent']:.2f}/{lim['max_daily_usd']:g} · terbuka ${t['open_usd']:.2f}/"
             f"{lim['max_open_usd']:g} · PnL {t['realized_pnl']:+.2f} (stop rugi ${lim['max_daily_loss']:g})"]
    skips = skip_summary()
    if skips:
        lines.append("Dilewati 24 jam: " + " · ".join(f"{k} {v}" for k, v in skips.items()) + " (`/copy dilewati`)")
    if not rows:
        lines.append("Belum ada wallet yang dilacak. `/track <alamat>` lalu `/copy <nama|alamat> on`.")
    for r in rows:
        c, s = r["copy"], r["stats"]
        name = md(r["name"] or r["address"][:10])
        state = "✅" if c["enabled"] else "⬜️"
        res = (f" · {s['trades']} copy · WR {s['win_rate'] * 100:.0f}% · PnL {s['pnl']:+.2f}" if s.get("win_rate") is not None
               else (f" · {s['trades']} copy · {s['open']} terbuka" if s["trades"] else ""))
        style = r.get("style") or {}
        warn = (f"\n   ⚠️ scalper: {style['scalp_ratio'] * 100:.0f}% beli dijual lagi ≤{SCALP_WINDOW_MIN} mnt — sulit di-copy"
                if style.get("scalper") else "")
        lines.append(f"{state} {name} · ${c['usd']:g}/trade · slip ≤{c['max_slippage'] * 100:.0f}¢ · telat ≤{c['max_delay_min']:g} mnt"
                     + (" · ikut jual" if c["mirror_sell"] else "") + res + warn)
    lines.append("\n`/copy start|stop` · `/copy <wallet> on [usd]` · `off` · `usd 5` · `slip 3` · `jual on|off` · `/copy dilewati`")
    return "\n".join(lines)


def format_skips(limit: int = 15) -> str:
    from app.paper_trading.wallet_bot import md

    rows = recent_skips(limit=limit)
    if not rows:
        return "⏭ Tidak ada copy yang dilewati dalam 24 jam terakhir."
    lines = [f"⏭ *Copy dilewati* (24 jam, {len(rows)} terakhir)"]
    for r in rows:
        lines.append(f"• {md(r['wallet_label'] or '-')} · {md((r['label'] or '').split(' · ', 1)[-1][:70])}\n  {md(r['reason'])}")
    return "\n".join(lines)


def handle_copy_command(args: List[str]) -> str:
    from app.paper_trading.wallet_bot import md
    from app.paper_trading.wallets import resolve_wallet

    if not args:
        return format_copy_list()
    if args[0].lower() in ("start", "stop", "mulai", "berhenti"):
        set_active(args[0].lower() in ("start", "mulai"))
        return ("🟢 Copy trade dijalankan." if is_active() else "🔴 Copy trade dihentikan.") + "\n\n" + format_copy_list()
    if args[0].lower() in ("dilewati", "skip", "skipped"):
        return format_skips()
    if len(args) < 2:
        return "Format: `/copy <wallet> on [usd]` · `off` · `usd <n>` · `slip <¢>` · `jual on|off`"
    try:
        address = resolve_wallet(args[0])
    except ValueError as err:
        return f"❌ {md(err)}"
    action, rest = args[1].lower(), args[2:]
    try:
        if action in ("on", "aktif", "start"):
            updates: Dict[str, Any] = {"enabled": True}
            if rest:
                updates["usd"] = rest[0]
        elif action in ("off", "mati", "stop"):
            updates = {"enabled": False}
        elif action == "usd" and rest:
            updates = {"usd": rest[0]}
        elif action in ("slip", "slippage") and rest:
            updates = {"max_slippage": float(rest[0]) / 100}
        elif action in ("jual", "sell") and rest:
            updates = {"mirror_sell": rest[0].lower() in ("on", "ya", "1", "true")}
        else:
            return "Aksi tidak dikenal. Pakai `on`, `off`, `usd <n>`, `slip <¢>`, atau `jual on|off`."
        c = set_settings(address, updates)
    except ValueError as err:
        return f"❌ {md(err)}"
    note = "" if is_active() else "\n⚠️ Copy trade sedang berhenti — `/copy start` untuk menjalankan."
    return (f"👛 Copy {md(address[:10])}… {'✅ AKTIF' if c['enabled'] else '⬜️ mati'} · ${c['usd']:g}/trade · "
            f"slip ≤{c['max_slippage'] * 100:.0f}¢ · harga {c['min_price'] * 100:.0f}–{c['max_price'] * 100:.0f}¢ · "
            f"{'ikut jual' if c['mirror_sell'] else 'tidak ikut jual'}{note}")

