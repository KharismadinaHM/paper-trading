"""
Copy trade (PAPER): ikut membeli otomatis saat wallet yang diikuti membeli, per wallet yang dipilih.

Alur: poll_followed_wallets (tiap WALLET_POLL_SECONDS) menemukan transaksi baru wallet yang diikuti →
process_wallet_trades() untuk wallet yang copy-nya aktif:
- BUY  → beli outcome yang sama di akun paper (strategi "copy") memakai harga order book TERKINI + slippage + fee,
         lewat risk check auto trader (batas harian, posisi terbuka, stop rugi). Sekali per wallet + market + outcome.
         Dilewati bila: transaksi wallet sudah terlalu lama, harga sudah naik > max_slippage dari harga wallet,
         harga di luar [min_price, max_price], spread terlalu lebar, atau auto trader / strategi "copy" mati.
- SELL → (bila mirror_sell) jual posisi copy kita di market + sisi yang sama.

Pengaturan per wallet disimpan di autotrade_state ("copy|<alamat>"), bisa diubah dari dashboard & Telegram (/copy).
Catatan: analisis 1.000+ market crypto Up/Down menunjukkan wallet top tidak konsisten setelah jeda 1 menit —
copy trade lebih masuk akal di market lambat (cuaca, politik, olahraga). Uji di paper dulu.
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.database import get_db_session
from app.core.logging import get_logger

logger = get_logger("copy_trader")

STRATEGY = "copy"
DEFAULTS = {"enabled": False, "usd": None, "max_slippage": 0.03, "min_price": 0.05, "max_price": 0.95,
            "mirror_sell": True, "max_delay_min": 15}
LIMITS = {"usd": (1, 1000), "max_slippage": (0, 0.5), "min_price": (0, 0.99), "max_price": (0.01, 0.99),
          "max_delay_min": (1, 1440)}


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
    return sorted(rows, key=lambda r: (not r["copy"]["enabled"], not r["follow"], (r["name"] or r["address"]).lower()))


def copy_stats() -> Dict[str, Dict[str, Any]]:
    """Per wallet: jumlah copy, selesai, menang, PnL terealisasi, posisi terbuka (strategi copy)."""
    from app.paper_trading.autotrader import STRATEGY_VERSIONS
    from app.paper_trading.models import AutotradeDecision, PaperTrade, PaperTradeStatus

    db = get_db_session()
    try:
        decisions = (db.query(AutotradeDecision)
                     .filter(AutotradeDecision.strategy == STRATEGY, AutotradeDecision.status == "filled").all())
        trades = (db.query(PaperTrade).filter(PaperTrade.strategy_version == STRATEGY_VERSIONS[STRATEGY]).all())
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
    """Catat copy yang dilewati (sekali per wallet + market + outcome) dan kabari singkat."""
    from app.paper_trading.autotrader import _record, notify

    _record(decision, "rejected", reason[:500], now)
    if not quiet:
        notify(f"⏭ COPY DILEWATI (paper) · 👛 {decision['features']['wallet_label']}\n{decision['title']}\nAlasan: {reason}")
    return {"action": "skipped", "reason": reason, "key": decision["key"]}


def copy_buy(wallet: Any, trade: Dict[str, Any], cfg_wallet: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
    """Salin satu pembelian wallet. Mengembalikan ringkasan hasil, atau None bila sudah pernah diproses."""
    from app.paper_trading.autotrader import _book_side, already_decided, cfg, execute

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
        return _skip(decision, "order book tidak tersedia / tidak cukup dalam", now)
    if price - wallet_price > float(cfg_wallet["max_slippage"]) + 1e-9:
        return _skip(decision, f"harga sudah naik {wallet_price * 100:.1f}¢ → {price * 100:.1f}¢ "
                               f"(maks +{float(cfg_wallet['max_slippage']) * 100:.0f}¢)", now)
    if price < float(cfg_wallet["min_price"]) or price > float(cfg_wallet["max_price"]):
        return _skip(decision, f"harga {price * 100:.1f}¢ di luar batas {float(cfg_wallet['min_price']) * 100:.0f}–"
                               f"{float(cfg_wallet['max_price']) * 100:.0f}¢", now)
    if book.get("spread") is not None and book["spread"] > float(cfg("MAX_SPREAD")):
        return _skip(decision, f"spread {book['spread'] * 100:.1f}¢ terlalu lebar", now)
    order = execute(decision, now)
    return {"action": "bought" if order else "rejected", "key": key}


def copy_sell(wallet: Any, trade: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
    """Wallet menjual: jual posisi copy kita (strategi copy) di market + sisi yang sama, bila ada."""
    from app.paper_service import sell_paper_position
    from app.paper_trading.autotrader import STRATEGY_VERSIONS, notify
    from app.paper_trading.models import PaperPosition, TradeSide

    side = "YES" if trade["outcome_index"] == 0 else "NO"
    db = get_db_session()
    try:
        pos = (db.query(PaperPosition).filter(PaperPosition.market_id == trade["condition_id"],
                                              PaperPosition.side == TradeSide(side),
                                              PaperPosition.strategy_version == STRATEGY_VERSIONS[STRATEGY],
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
    from app.paper_trading.autotrader import enabled_strategies, is_enabled

    now = now or datetime.now(timezone.utc)
    results: List[Dict[str, Any]] = []
    try:
        cfg_wallet = get_settings(wallet.address)
        if not cfg_wallet["enabled"] or not rows:
            return results
        if not is_enabled() or STRATEGY not in enabled_strategies():
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
    """/copy: status copy trade per wallet."""
    from app.paper_trading.autotrader import enabled_strategies, is_enabled
    from app.paper_trading.wallet_bot import md

    rows = list_copy()
    active = is_enabled() and STRATEGY in enabled_strategies()
    lines = ["👛 *Copy trade (paper)* — " + ("🟢 aktif" if active else
             "🔴 nonaktif (" + ("auto trader berhenti" if not is_enabled() else "strategi 'copy' belum dicentang") + ")")]
    if not rows:
        lines.append("Belum ada wallet yang dilacak. `/track <alamat>` lalu `/copy <nama|alamat> on`.")
    for r in rows:
        c, s = r["copy"], r["stats"]
        name = md(r["name"] or r["address"][:10])
        state = "✅" if c["enabled"] else "⬜️"
        res = (f" · {s['trades']} copy · WR {s['win_rate'] * 100:.0f}% · PnL {s['pnl']:+.2f}" if s.get("win_rate") is not None
               else (f" · {s['trades']} copy · {s['open']} terbuka" if s["trades"] else ""))
        lines.append(f"{state} {name} · ${c['usd']:g}/trade · slip ≤{c['max_slippage'] * 100:.0f}¢"
                     + (" · ikut jual" if c["mirror_sell"] else "") + res)
    lines.append("\n`/copy <wallet> on [usd]` · `/copy <wallet> off` · `/copy <wallet> usd 5` · "
                 "`/copy <wallet> slip 3` · `/copy <wallet> jual on|off`")
    return "\n".join(lines)


def handle_copy_command(args: List[str]) -> str:
    from app.paper_trading.wallet_bot import md
    from app.paper_trading.wallets import resolve_wallet

    if not args:
        return format_copy_list()
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
    note = ""
    from app.paper_trading.autotrader import enabled_strategies
    if c["enabled"] and STRATEGY not in enabled_strategies():
        note = "\n⚠️ Strategi `copy` belum dicentang di Auto Bot → Pengaturan — belum ada yang dibeli."
    return (f"👛 Copy {md(address[:10])}… {'✅ AKTIF' if c['enabled'] else '⬜️ mati'} · ${c['usd']:g}/trade · "
            f"slip ≤{c['max_slippage'] * 100:.0f}¢ · harga {c['min_price'] * 100:.0f}–{c['max_price'] * 100:.0f}¢ · "
            f"{'ikut jual' if c['mirror_sell'] else 'tidak ikut jual'}{note}")

