"""
Wallet tracker Polymarket: cari wallet menarik, lacak statistiknya, dan ikuti transaksinya.

Sumber (publik, tanpa API key):
- Leaderboard: data-api /v1/leaderboard (kategori WEATHER, per bulan) → kandidat wallet.
- Statistik: /closed-positions (urut waktu — default API urut PnL sehingga bias), /positions
  (posisi resolve yang belum di-redeem: harga 0 = kalah, 1 = menang), /activity, /value.
- Alert: /activity?type=TRADE untuk wallet yang diikuti, dicek tiap WALLET_POLL_SECONDS.

Win rate = posisi untung / posisi selesai (ditutup atau sudah resolve) dalam WALLET_STATS_DAYS hari.
"""
import json
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.models import TrackedWallet, WalletAlertLog, WalletCandidate

logger = get_logger("wallets")

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
STATS_TTL = 10 * 60
WEATHER_RE = re.compile(r"temperature|weather|rain|snow|hurricane", re.I)


class WalletError(ValueError):
    pass


# --- HTTP ---------------------------------------------------------------------------------

def _get(path: str, **params) -> Any:
    from app.paper_trading.live_market_data import _http

    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return json.loads(_http(f"{DATA_API}{path}?{query}", timeout=20) or "null")


def normalize_address(value: str) -> str:
    """Terima alamat 0x… atau URL profil polymarket.com/profile/0x…; kembalikan lowercase."""
    match = ADDRESS_RE.search(str(value or ""))
    if not match:
        raise WalletError("Alamat wallet tidak valid (harus 0x + 40 karakter hex).")
    return match.group(0).lower()


def short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


def profile_url(address: str) -> str:
    return f"https://polymarket.com/profile/{address}"


# --- Statistik ----------------------------------------------------------------------------

def _closed_positions(address: str, cutoff_ts: int, max_pages: int = 6):
    """
    Posisi yang ditutup sejak cutoff (urut terbaru). Jika sampel terpotong oleh max_pages, kembalikan
    juga cutoff efektif = waktu posisi tertua dalam sampel, agar sumber lain dibatasi rentang yang sama.
    """
    rows: List[Dict[str, Any]] = []
    truncated = False
    for page in range(max_pages):
        batch = _get("/closed-positions", user=address, limit=50, offset=page * 50,
                     sortBy="TIMESTAMP", sortDirection="DESC") or []
        rows.extend(batch)
        if len(batch) < 50 or (batch and int(batch[-1].get("timestamp") or 0) < cutoff_ts):
            break
    else:
        truncated = True
    rows = [r for r in rows if int(r.get("timestamp") or 0) >= cutoff_ts]
    effective = min((int(r.get("timestamp") or 0) for r in rows), default=cutoff_ts) if truncated else cutoff_ts
    return rows, effective


ANON_NAME_RE = re.compile(r"^0x[a-fA-F0-9]{40}(-\d+)?$")


def clean_name(name: Optional[str]) -> Optional[str]:
    """Nama tampilan; None untuk nama default anonim ('0x…' atau '0x…-1789…')."""
    name = str(name or "").strip()
    return None if not name or ANON_NAME_RE.match(name) else name


def leaderboard_entry(address: str, period: str = "MONTH", category: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        rows = _get("/v1/leaderboard", user=address, timePeriod=period, category=category) or []
    except Exception:
        return None
    if not rows:
        return None
    r = rows[0]
    return {"rank": int(r.get("rank") or 0), "pnl": float(r.get("pnl") or 0), "vol": float(r.get("vol") or 0),
            "name": clean_name(r.get("userName"))}


def _parse_end(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def compute_stats(address: str, days: Optional[int] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Statistik wallet dalam `days` hari terakhir (default WALLET_STATS_DAYS)."""
    now = now or datetime.now(timezone.utc)
    days = days or settings.WALLET_STATS_DAYS
    cutoff = now - timedelta(days=days)
    cutoff_ts = int(cutoff.timestamp())

    closed, effective_ts = _closed_positions(address, cutoff_ts)
    effective = datetime.fromtimestamp(effective_ts, timezone.utc)
    positions = _get("/positions", user=address, limit=500, sizeThreshold=0.1) or []
    activity = _get("/activity", user=address, limit=100, type="TRADE") or []
    try:
        value = float((_get("/value", user=address) or [{}])[0].get("value") or 0)
    except Exception:
        value = None

    results = [float(r.get("realizedPnl") or 0) for r in closed]
    for p in positions:  # resolve tapi belum di-redeem (kalah biasanya tidak pernah di-redeem)
        end = _parse_end(p.get("endDate"))
        if p.get("redeemable") and (end is None or end >= effective):
            results.append(float(p.get("cashPnl") or 0))
    # Rata-rata harga beli (tertimbang nilai beli) posisi dalam sampel: ≥85¢ = pola "hampir pasti"
    bought = [(float(r.get("avgPrice") or 0), float(r.get("totalBought") or 0)) for r in closed]
    weight = sum(w for _, w in bought)
    avg_entry = round(sum(pr * w for pr, w in bought) / weight, 4) if weight else None
    wins = sum(1 for r in results if r > 0)
    losses = sum(1 for r in results if r < 0)
    resolved = wins + losses
    open_positions = [p for p in positions if not p.get("redeemable")]

    name = next((clean_name(a.get("name")) or clean_name(a.get("pseudonym")) for a in activity
                 if clean_name(a.get("name")) or clean_name(a.get("pseudonym"))), None)
    month = leaderboard_entry(address, "MONTH")
    alltime = leaderboard_entry(address, "ALL")
    recent = [a for a in activity if int(a.get("timestamp") or 0) >= cutoff_ts]
    weather = sum(1 for a in recent if WEATHER_RE.search(str(a.get("title") or "")))
    last_ts = max((int(a.get("timestamp") or 0) for a in activity), default=None)
    volume = sum(float(a.get("usdcSize") or 0) for a in recent)
    return {
        "address": address,
        "name": name or (month or {}).get("name"),
        "days": days,
        # rentang sampel win rate (lebih pendek dari `days` jika wallet sangat aktif)
        "sample_days": round((now - effective).total_seconds() / 86400, 1),
        "wins": wins,
        "losses": losses,
        "resolved": resolved,
        "win_rate": round(wins / resolved, 4) if resolved else None,
        "sample_pnl": round(sum(results), 2),
        # PnL resmi leaderboard Polymarket (bulan ini & sepanjang waktu); fallback ke PnL sampel
        "pnl": round(month["pnl"], 2) if month else round(sum(results), 2),
        "pnl_all": round(alltime["pnl"], 2) if alltime else None,
        "volume_month": round(month["vol"], 2) if month else None,
        # margin = PnL / volume bulan ini (keuntungan per $ yang diperdagangkan)
        "margin": round(month["pnl"] / month["vol"], 4) if month and month["vol"] else None,
        "avg_entry": avg_entry,
        "open_positions": len(open_positions),
        "open_value": round(sum(float(p.get("currentValue") or 0) for p in open_positions), 2),
        "portfolio_value": value,
        "trades_sampled": len(recent),
        "recent_volume": round(volume, 2),
        "weather_share": round(weather / len(recent), 2) if recent else None,
        "last_trade_ts": last_ts,
        "computed_at": now.isoformat(),
    }


def get_stats(address: str, refresh: bool = False) -> Dict[str, Any]:
    from app.paper_trading.live_market_data import _cache, _cached, _lock

    key = f"wallet_stats:{address}"
    if refresh:
        with _lock:
            _cache.pop(key, None)
    return _cached(key, STATS_TTL, lambda: compute_stats(address))


def recent_activity(address: str, limit: int = 10) -> List[Dict[str, Any]]:
    rows = _get("/activity", user=address, limit=limit) or []
    return [{
        "timestamp": int(r.get("timestamp") or 0), "type": r.get("type"), "side": r.get("side"),
        "outcome": r.get("outcome"), "title": r.get("title"), "price": r.get("price"),
        "size": r.get("size"), "usdc": r.get("usdcSize"), "event_slug": r.get("eventSlug"),
        "transaction_hash": r.get("transactionHash"),
    } for r in rows]


def ago(ts: Optional[int], now: Optional[datetime] = None) -> str:
    if not ts:
        return "-"
    seconds = max(0, int(((now or datetime.now(timezone.utc)).timestamp()) - ts))
    if seconds < 120:
        return "baru saja"
    if seconds < 3600:
        return f"{seconds // 60} menit lalu"
    if seconds < 86400:
        return f"{seconds // 3600} jam lalu"
    return f"{seconds // 86400} hari lalu"


def money(value: Optional[float]) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else ("-" if value < 0 else "")
    v = abs(value)
    body = f"${v / 1e6:.2f}M" if v >= 1e6 else (f"${v / 1e3:.1f}K" if v >= 1e3 else f"${v:.0f}")
    return sign + body


def reason_for(stats: Dict[str, Any], leaderboard: Optional[Dict[str, Any]] = None) -> str:
    """Alasan rekomendasi dalam satu kalimat."""
    parts = []
    if leaderboard:
        parts.append(f"peringkat #{leaderboard['rank']} PnL cuaca bulan ini ({money(leaderboard.get('pnl'))} "
                     f"dari volume {money(leaderboard.get('vol')).lstrip('+')})")
    if stats.get("win_rate") is not None:
        span = stats.get("sample_days") or stats["days"]
        parts.append(f"win rate {stats['win_rate'] * 100:.0f}% ({stats['wins']}/{stats['resolved']} posisi selesai, "
                     f"{span:g} hari terakhir)")
        if stats["win_rate"] < 0.4 and (stats.get("pnl") or 0) > 0:
            parts.append("win rate rendah tapi untung — pola beli bracket murah yang sesekali menang besar")
    if stats.get("margin") is not None:
        parts.append(f"margin {stats['margin'] * 100:.1f}% dari volume")
    if stats.get("avg_entry") is not None:
        entry = stats["avg_entry"] * 100
        if entry >= 85:
            parts.append(f"rata-rata beli {entry:.0f}¢ — pola 'hampir pasti': sering menang, untung tipis per posisi")
        elif entry <= 30:
            parts.append(f"rata-rata beli {entry:.0f}¢ — pola bracket murah")
        else:
            parts.append(f"rata-rata beli {entry:.0f}¢")
    if stats.get("weather_share") is not None:
        parts.append(f"{stats['weather_share'] * 100:.0f}% transaksi di market cuaca")
    if stats.get("last_trade_ts"):
        parts.append(f"aktif {ago(stats['last_trade_ts'])}")
    return "; ".join(parts)


# --- Discovery ----------------------------------------------------------------------------

def fetch_leaderboard(limit: int = 25) -> List[Dict[str, Any]]:
    rows = _get("/v1/leaderboard", category=settings.WALLET_DISCOVERY_CATEGORY, timePeriod="MONTH",
                orderBy="PNL", limit=limit) or []
    return [{"address": str(r.get("proxyWallet") or "").lower(), "name": clean_name(r.get("userName")),
             "rank": int(r.get("rank") or 0), "pnl": float(r.get("pnl") or 0), "vol": float(r.get("vol") or 0)}
            for r in rows if r.get("proxyWallet")]


def score(stats: Dict[str, Any]) -> float:
    """
    Setengah dari win rate yang dihaluskan (Laplace, dikali keyakinan jumlah posisi), setengah dari
    margin PnL/volume (10% = penuh) — agar wallet 'beli 99¢' yang untungnya tipis tidak selalu di atas.
    """
    resolved = stats.get("resolved") or 0
    if not resolved:
        return 0.0
    smoothed = (stats["wins"] + 1) / (resolved + 2)
    confidence = min(resolved / 50, 1.0)
    wr_part = smoothed * (0.5 + 0.5 * confidence)
    margin = stats.get("margin")
    margin_part = min(max(margin / 0.10, 0.0), 1.0) if margin is not None else wr_part
    profitable = 1.0 if (stats.get("pnl") or 0) > 0 else 0.5
    return round((0.5 * wr_part + 0.5 * margin_part) * profitable, 6)


def discover_wallets(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Hitung ulang kandidat wallet menarik dan simpan ke wallet_candidates."""
    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    try:
        excluded = {w.address for w in db.query(TrackedWallet)}
    finally:
        db.close()
    board = [r for r in fetch_leaderboard(limit=settings.WALLET_DISCOVERY_CANDIDATES * 2) if r["address"] not in excluded]
    board = board[:settings.WALLET_DISCOVERY_CANDIDATES]

    def evaluate(entry):
        try:
            return entry, compute_stats(entry["address"], now=now)
        except Exception as err:
            logger.warning("Gagal menghitung statistik wallet %s: %s", entry["address"], err)
            return entry, None

    with ThreadPoolExecutor(max_workers=4) as pool:
        evaluated = list(pool.map(evaluate, board))
    min_resolved = settings.WALLET_DISCOVERY_MIN_RESOLVED
    stale = int((now - timedelta(days=7)).timestamp())
    candidates = []
    for entry, stats in evaluated:
        if not stats or (stats["resolved"] or 0) < min_resolved or (stats["last_trade_ts"] or 0) < stale:
            continue
        stats["name"] = stats.get("name") or clean_name(entry["name"])
        candidates.append({"address": entry["address"], "name": stats["name"], "score": score(stats),
                           "reason": reason_for(stats, entry), "stats": stats, "leaderboard": entry})
    candidates.sort(key=lambda c: -c["score"])

    db = get_db_session()
    try:
        db.query(WalletCandidate).delete()
        for rank, c in enumerate(candidates, start=1):
            db.add(WalletCandidate(address=c["address"], name=c["name"], rank=rank, score=Decimal(str(c["score"])),
                                   reason=c["reason"], stats_json=json.dumps({**c["stats"], "leaderboard": c["leaderboard"]}),
                                   discovered_at=now))
        db.commit()
    finally:
        db.close()
    logger.info("Discovery wallet: %d kandidat", len(candidates))
    return list_candidates()


def list_candidates() -> List[Dict[str, Any]]:
    db = get_db_session()
    try:
        tracked = {w.address for w in db.query(TrackedWallet)}
        rows = db.query(WalletCandidate).order_by(WalletCandidate.rank).all()
        return [{"address": r.address, "name": r.name, "rank": r.rank, "reason": r.reason,
                 "stats": json.loads(r.stats_json or "{}"), "discovered_at": r.discovered_at}
                for r in rows if r.address not in tracked]
    finally:
        db.close()


def candidates_age(now: Optional[datetime] = None) -> Optional[timedelta]:
    db = get_db_session()
    try:
        latest = db.query(WalletCandidate.discovered_at).order_by(WalletCandidate.discovered_at.desc()).first()
    finally:
        db.close()
    if not latest:
        return None
    at = latest[0] if latest[0].tzinfo else latest[0].replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - at


def refresh_candidates_if_stale(now: Optional[datetime] = None) -> None:
    age = candidates_age(now)
    if age is None or age > timedelta(hours=settings.WALLET_DISCOVERY_REFRESH_HOURS):
        discover_wallets(now=now)


# --- Tracking / follow --------------------------------------------------------------------

def _serialize(w: TrackedWallet) -> Dict[str, Any]:
    return {"address": w.address, "name": w.name, "status": w.status, "follow": w.follow, "source": w.source,
            "added_at": w.added_at, "stats": json.loads(w.stats_json or "{}"), "stats_at": w.stats_at}


def list_tracked(include_skipped: bool = False) -> List[Dict[str, Any]]:
    db = get_db_session()
    try:
        query = db.query(TrackedWallet).order_by(TrackedWallet.added_at)
        if not include_skipped:
            query = query.filter(TrackedWallet.status != "skipped")
        return [_serialize(w) for w in query]
    finally:
        db.close()


def resolve_wallet(query: str) -> str:
    """Alamat dari input: 0x…, URL profil, nama wallet yang dilacak/kandidat, atau nomor kandidat /discover."""
    text = str(query or "").strip()
    if ADDRESS_RE.search(text):
        return normalize_address(text)
    candidates = list_candidates()
    if text.isdigit():
        for c in candidates:
            if c["rank"] == int(text):
                return c["address"]
    for item in list_tracked(include_skipped=True) + candidates:
        if item.get("name") and item["name"].lower() == text.lower():
            return item["address"]
    raise WalletError(f"Wallet '{text}' tidak ditemukan. Pakai alamat 0x…, nama, atau nomor dari /discover.")


def track_wallet(address: str, follow: bool = False, source: str = "manual",
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """Mulai lacak (dan opsional ikuti) wallet; statistik dihitung saat itu juga."""
    address = normalize_address(address)
    now = now or datetime.now(timezone.utc)
    stats = get_stats(address, refresh=True)
    db = get_db_session()
    try:
        wallet = db.get(TrackedWallet, address)
        if wallet is None:
            wallet = TrackedWallet(address=address, added_at=now, source=source)
            db.add(wallet)
        wallet.status = "tracking"
        wallet.name = stats.get("name") or wallet.name
        wallet.stats_json = json.dumps(stats)
        wallet.stats_at = now
        if follow and not wallet.follow:
            wallet.follow = True
            wallet.last_activity_ts = int(now.timestamp())  # alert hanya transaksi setelah mulai diikuti
        db.commit()
        return _serialize(wallet)
    finally:
        db.close()


def set_follow(address: str, follow: bool, now: Optional[datetime] = None) -> Dict[str, Any]:
    address = normalize_address(address)
    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    try:
        wallet = db.get(TrackedWallet, address)
    finally:
        db.close()
    if wallet is None or wallet.status == "skipped":
        return track_wallet(address, follow=follow, source="discover" if follow else "manual", now=now)
    db = get_db_session()
    try:
        wallet = db.get(TrackedWallet, address)
        if follow and not wallet.follow:
            wallet.last_activity_ts = int(now.timestamp())
        wallet.follow = follow
        db.commit()
        return _serialize(wallet)
    finally:
        db.close()


def skip_wallet(address: str, now: Optional[datetime] = None) -> None:
    """Sembunyikan wallet dari rekomendasi (dan hentikan follow bila ada)."""
    address = normalize_address(address)
    db = get_db_session()
    try:
        wallet = db.get(TrackedWallet, address)
        if wallet is None:
            candidate = db.get(WalletCandidate, address)
            wallet = TrackedWallet(address=address, added_at=now or datetime.now(timezone.utc), source="discover",
                                   name=candidate.name if candidate else None)
            db.add(wallet)
        wallet.status, wallet.follow = "skipped", False
        db.commit()
    finally:
        db.close()


def untrack_wallet(address: str) -> bool:
    address = normalize_address(address)
    db = get_db_session()
    try:
        deleted = db.query(TrackedWallet).filter_by(address=address).delete()
        db.commit()
        return bool(deleted)
    finally:
        db.close()


def refresh_tracked_stats(now: Optional[datetime] = None) -> int:
    """Perbarui statistik wallet yang dilacak (dipanggil berkala)."""
    now = now or datetime.now(timezone.utc)
    count = 0
    for w in list_tracked():
        stats_at = w["stats_at"]
        if stats_at is not None:
            stats_at = stats_at if stats_at.tzinfo else stats_at.replace(tzinfo=timezone.utc)
            if now - stats_at < timedelta(hours=1):
                continue
        try:
            stats = get_stats(w["address"], refresh=True)
        except Exception as err:
            logger.warning("Gagal memperbarui statistik %s: %s", w["address"], err)
            continue
        db = get_db_session()
        try:
            wallet = db.get(TrackedWallet, w["address"])
            if wallet:
                wallet.stats_json, wallet.stats_at = json.dumps(stats), now
                wallet.name = stats.get("name") or wallet.name
                db.commit()
                count += 1
        finally:
            db.close()
    return count


# --- Alert transaksi ----------------------------------------------------------------------

def _label(wallet: TrackedWallet) -> str:
    return f"{wallet.name} ({short(wallet.address)})" if wallet.name else short(wallet.address)


def format_trade_alert(wallet: TrackedWallet, trades: List[Dict[str, Any]]) -> str:
    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    label = settings.NOTIFY_TIMEZONE_LABEL
    lines = [f"👛 {_label(wallet)} bertransaksi:"]
    for t in trades[:6]:
        at = datetime.fromtimestamp(t["timestamp"], tz)
        price = f"{float(t['price']) * 100:.1f}¢" if t.get("price") is not None else "-"
        lines.append(f"• {t['side']} {t.get('outcome') or ''} — {t.get('title')}".replace("  ", " "))
        lines.append(f"  {float(t['size']):,.1f} shares @ {price} (${float(t['usdc']):,.2f}) · {at:%H:%M} {label}")
        if t.get("event_slug"):
            lines.append(f"  https://polymarket.com/event/{t['event_slug']}")
    if len(trades) > 6:
        lines.append(f"… dan {len(trades) - 6} transaksi lain")
    lines.append(profile_url(wallet.address))
    return "\n".join(lines)


def _group_trades(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Gabungkan fill kecil ke market + sisi + outcome yang sama (satu order sering terisi berkali-kali)."""
    groups: Dict[tuple, Dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: int(r.get("timestamp") or 0)):
        key = (r.get("conditionId"), r.get("side"), r.get("outcome"))
        g = groups.setdefault(key, {"side": r.get("side"), "outcome": r.get("outcome"), "title": r.get("title"),
                                    "event_slug": r.get("eventSlug"), "size": 0.0, "usdc": 0.0,
                                    "timestamp": int(r.get("timestamp") or 0)})
        g["size"] += float(r.get("size") or 0)
        g["usdc"] += float(r.get("usdcSize") or 0)
        g["timestamp"] = max(g["timestamp"], int(r.get("timestamp") or 0))
    for g in groups.values():
        g["price"] = g["usdc"] / g["size"] if g["size"] else None
    return sorted(groups.values(), key=lambda g: g["timestamp"])


def poll_followed_wallets(now: Optional[datetime] = None) -> int:
    """Cek transaksi baru wallet yang diikuti dan kirim alert Telegram. Kembalikan jumlah alert terkirim."""
    from app.paper_trading.telegram import send_telegram_message

    if not settings.WALLET_ALERTS:
        return 0
    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    sent = 0
    try:
        for wallet in db.query(TrackedWallet).filter_by(follow=True, status="tracking").all():
            cursor = wallet.last_activity_ts or int(now.timestamp())
            try:
                rows = _get("/activity", user=wallet.address, limit=50, type="TRADE", start=cursor) or []
            except Exception as err:
                logger.warning("Gagal mengambil aktivitas %s: %s", wallet.address, err)
                continue
            fresh = []
            for r in rows:
                ts, tx, asset = int(r.get("timestamp") or 0), r.get("transactionHash"), str(r.get("asset") or "")
                if ts < cursor or not tx or db.get(WalletAlertLog, (tx, asset)) is not None:
                    continue
                if settings.WALLET_ALERT_WEATHER_ONLY and not WEATHER_RE.search(str(r.get("title") or "")):
                    continue
                fresh.append(r)
            if not fresh:
                continue
            trades = [t for t in _group_trades(fresh) if t["usdc"] >= settings.WALLET_ALERT_MIN_USDC]
            if trades:
                result = send_telegram_message(format_trade_alert(wallet, trades))
                if not result.get("success"):
                    logger.warning("Alert wallet tidak terkirim: %s", result.get("error"))
                    continue  # coba lagi siklus berikutnya
                sent += 1
            for r in fresh:
                db.add(WalletAlertLog(transaction_hash=r["transactionHash"], asset=str(r.get("asset") or ""),
                                      address=wallet.address, timestamp=int(r.get("timestamp") or 0)))
            wallet.last_activity_ts = max(int(r.get("timestamp") or 0) for r in fresh)
            db.commit()
        return sent
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def run_wallet_polling() -> int:
    """Dipanggil dari loop collector; tidak pernah melempar exception."""
    try:
        return poll_followed_wallets()
    except Exception as err:
        logger.error("Gagal polling wallet: %s", err, exc_info=True)
        return 0


def run_wallet_maintenance() -> None:
    """Refresh kandidat (tiap WALLET_DISCOVERY_REFRESH_HOURS) & statistik wallet yang dilacak."""
    try:
        refresh_candidates_if_stale()
    except Exception as err:
        logger.error("Gagal discovery wallet: %s", err, exc_info=True)
    try:
        refresh_tracked_stats()
    except Exception as err:
        logger.error("Gagal refresh statistik wallet: %s", err, exc_info=True)
