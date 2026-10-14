"""
Portfolio Polymarket milik sendiri — READ-ONLY.

- Data publik (cukup POLYMARKET_WALLET_ADDRESS, tanpa key): nilai posisi, PnL harian/mingguan/
  bulanan/all-time (leaderboard), posisi aktif, posisi resolve (siap di-redeem / kalah), riwayat
  aktivitas, win rate.
- Opsional, dengan API key CLOB (POLYMARKET_API_KEY/SECRET/PASSPHRASE di .env): saldo cash USDC dan
  open order. Hanya request GET yang ditandatangani (HMAC L2) — modul ini tidak pernah membuat,
  mengubah, atau membatalkan order, dan tidak butuh private key.
"""
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("my_wallet")

CLOB_HOST = "https://clob.polymarket.com"
PERIODS = (("DAY", "hari ini"), ("WEEK", "7 hari"), ("MONTH", "30 hari"), ("ALL", "all-time"))
SUMMARY_TTL = 60


def wallet_address() -> Optional[str]:
    from app.paper_trading.wallets import WalletError, normalize_address

    raw = settings.POLYMARKET_WALLET_ADDRESS
    if not raw:
        return None
    try:
        return normalize_address(raw)
    except WalletError:
        logger.warning("POLYMARKET_WALLET_ADDRESS tidak valid")
        return None


def clob_configured() -> bool:
    return bool(settings.POLYMARKET_API_KEY and settings.POLYMARKET_API_SECRET and settings.POLYMARKET_API_PASSPHRASE)


# --- CLOB L2 (read-only) ------------------------------------------------------------------

def l2_signature(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    """HMAC-SHA256 header POLY_SIGNATURE sesuai spesifikasi L2 CLOB (secret base64url)."""
    key = base64.urlsafe_b64decode(secret)
    message = f"{timestamp}{method}{request_path}{body}"
    return base64.urlsafe_b64encode(hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()).decode("utf-8")


def _clob_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """GET terautentikasi ke CLOB. Tanda tangan dihitung atas path tanpa query string."""
    if not clob_configured():
        raise RuntimeError("API key CLOB belum diatur")
    timestamp = str(int(time.time()))
    headers = {
        "POLY_ADDRESS": settings.POLYMARKET_SIGNER_ADDRESS or settings.POLYMARKET_WALLET_ADDRESS or "",
        "POLY_SIGNATURE": l2_signature(settings.POLYMARKET_API_SECRET, timestamp, "GET", path),
        "POLY_TIMESTAMP": timestamp,
        "POLY_API_KEY": settings.POLYMARKET_API_KEY,
        "POLY_PASSPHRASE": settings.POLYMARKET_API_PASSPHRASE,
        "User-Agent": "Mozilla/5.0 (paper-trading read-only)",
        "Accept": "application/json",
    }
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    req = urllib.request.Request(f"{CLOB_HOST}{path}{query}", headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8") or "null")


def fetch_cash_balance() -> Optional[float]:
    """Saldo USDC (collateral) yang bisa dipakai trading, dari CLOB. None jika tidak tersedia."""
    if not clob_configured():
        return None
    try:
        data = _clob_get("/balance-allowance", {"asset_type": "COLLATERAL",
                                                "signature_type": settings.POLYMARKET_SIGNATURE_TYPE})
        return round(int(data.get("balance") or 0) / 1e6, 2)
    except Exception as err:
        logger.warning("Gagal mengambil saldo CLOB: %s", type(err).__name__)
        return None


def fetch_open_orders() -> Optional[List[Dict[str, Any]]]:
    """Open order (limit order yang belum terisi penuh). None jika key tidak diatur / gagal."""
    if not clob_configured():
        return None
    try:
        orders, cursor = [], None
        for _ in range(5):
            data = _clob_get("/data/orders", {"next_cursor": cursor} if cursor else None)
            rows = data.get("data", []) if isinstance(data, dict) else (data or [])
            orders.extend(rows)
            cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not cursor or cursor == "LTE=":
                break
    except Exception as err:
        logger.warning("Gagal mengambil open order CLOB: %s", type(err).__name__)
        return None
    out = []
    for o in orders:
        size = float(o.get("original_size") or 0)
        matched = float(o.get("size_matched") or 0)
        price = float(o.get("price") or 0)
        out.append({"id": o.get("id"), "side": o.get("side"), "outcome": o.get("outcome"), "price": price,
                    "size": size, "filled": matched, "remaining_usdc": round((size - matched) * price, 2),
                    "market": o.get("market"), "created_at": o.get("created_at"), "status": o.get("status")})
    return out


# --- Ringkasan ----------------------------------------------------------------------------

def _period_pnl(address: str) -> Dict[str, Dict[str, Optional[float]]]:
    from app.paper_trading.wallets import leaderboard_entry

    out = {}
    for period, _ in PERIODS:
        entry = leaderboard_entry(address, period)
        out[period] = {"pnl": round(entry["pnl"], 2) if entry else None, "volume": round(entry["vol"], 2) if entry else None}
    return out


def _position_row(p: Dict[str, Any]) -> Dict[str, Any]:
    from app.paper_trading.wallets import _parse_end

    end = _parse_end(p.get("endDate"))
    return {
        "title": p.get("title"), "outcome": p.get("outcome"), "size": round(float(p.get("size") or 0), 2),
        "avg_price": float(p.get("avgPrice") or 0), "cur_price": float(p.get("curPrice") or 0),
        "value": round(float(p.get("currentValue") or 0), 2), "cost": round(float(p.get("initialValue") or 0), 2),
        "pnl": round(float(p.get("cashPnl") or 0), 2), "pnl_pct": round(float(p.get("percentPnl") or 0), 1),
        "end_date": end.isoformat() if end else None, "redeemable": bool(p.get("redeemable")),
        "url": f"https://polymarket.com/event/{p['eventSlug']}" if p.get("eventSlug") else None,
    }


def insights(summary: Dict[str, Any], now: Optional[datetime] = None) -> List[str]:
    """Saran/ringkasan otomatis dari kondisi portfolio (informasi, bukan saran finansial)."""
    from app.paper_trading.wallets import money

    now = now or datetime.now(timezone.utc)
    tips: List[str] = []
    claim = summary["claimable"]
    if claim["count"]:
        tips.append(f"💰 {claim['count']} posisi menang siap di-redeem senilai {money(claim['value']).lstrip('+')} — "
                    "klaim di Polymarket supaya dananya kembali ke cash.")
    active = summary["positions"]
    soon = [p for p in active if p["end_date"]
            and timedelta(0) <= datetime.fromisoformat(p["end_date"]) - now <= timedelta(hours=24)]
    if soon:
        tips.append(f"⏳ {len(soon)} posisi berakhir dalam 24 jam (total nilai {money(sum(p['value'] for p in soon)).lstrip('+')}).")
    total = sum(p["value"] for p in active)
    if active and total > 0:
        top = max(active, key=lambda p: p["value"])
        share = top["value"] / total
        if share >= 0.3 and len(active) > 1:
            tips.append(f"⚖️ {share * 100:.0f}% nilai posisi ada di satu market ({top['title']}) — konsentrasi tinggi.")
    losers = [p for p in active if p["pnl_pct"] <= -50 and p["cost"] >= 1]
    if losers:
        tips.append(f"📉 {len(losers)} posisi turun lebih dari 50% dari harga beli.")
    near = [p for p in active if p["cur_price"] >= 0.95]
    if near:
        tips.append(f"✅ {len(near)} posisi di harga ≥95¢ (pasar menilai hampir pasti menang).")
    if summary["lost"]["count"]:
        tips.append(f"🧹 {summary['lost']['count']} posisi kalah (harga 0) masih tercatat — tidak perlu tindakan.")
    orders = summary.get("open_orders")
    if orders:
        locked = sum(o["remaining_usdc"] for o in orders if (o.get("side") or "").upper() == "BUY")
        tips.append(f"📝 {len(orders)} open order; {money(locked).lstrip('+')} tertahan di order BUY.")
    stats = summary.get("stats") or {}
    if stats.get("win_rate") is not None and stats.get("avg_entry") is not None:
        tips.append(f"📊 Win rate {stats['win_rate'] * 100:.0f}% dengan rata-rata beli {stats['avg_entry'] * 100:.0f}¢ "
                    f"({stats['resolved']} posisi selesai).")
    return tips


def build_summary(now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Ringkasan lengkap portfolio (None jika POLYMARKET_WALLET_ADDRESS belum diatur)."""
    from app.paper_trading.wallets import _get, compute_stats, recent_activity

    address = wallet_address()
    if not address:
        return None
    now = now or datetime.now(timezone.utc)
    positions = _get("/positions", user=address, limit=500, sizeThreshold=0.01) or []
    rows = [_position_row(p) for p in positions]
    active = sorted([r for r in rows if not r["redeemable"]], key=lambda r: -r["value"])
    claimable = [r for r in rows if r["redeemable"] and r["cur_price"] >= 0.99]
    lost = [r for r in rows if r["redeemable"] and r["cur_price"] <= 0.01]
    try:
        value = float((_get("/value", user=address) or [{}])[0].get("value") or 0)
    except Exception:
        value = sum(r["value"] for r in rows)
    try:
        stats = compute_stats(address, now=now)
    except Exception as err:
        logger.warning("Gagal menghitung statistik wallet sendiri: %s", err)
        stats = None
    summary = {
        "address": address,
        "positions_value": round(value, 2),
        "cash": fetch_cash_balance(),
        "clob_configured": clob_configured(),
        "pnl": _period_pnl(address),
        "unrealized_pnl": round(sum(r["pnl"] for r in active), 2),
        "positions": active,
        "claimable": {"count": len(claimable), "value": round(sum(r["value"] for r in claimable), 2), "items": claimable},
        "lost": {"count": len(lost)},
        "open_orders": fetch_open_orders(),
        "activity": recent_activity(address, limit=20),
        "stats": stats,
        "computed_at": now.isoformat(),
    }
    summary["insights"] = insights(summary, now=now)
    return summary


def get_summary(refresh: bool = False) -> Optional[Dict[str, Any]]:
    from app.paper_trading.live_market_data import _cache, _cached, _lock

    if not wallet_address():
        return None
    if refresh:
        with _lock:
            _cache.pop("my_wallet", None)
    return _cached("my_wallet", SUMMARY_TTL, build_summary)
