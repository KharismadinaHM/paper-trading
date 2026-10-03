"""
Deteksi "insider wallet": taruhan besar dengan pola yang sering muncul pada trader yang tahu lebih dulu.

Sumber (data publik Polymarket): trade taker besar terbaru di semua market (data-api /trades, filter
nominal), profil wallet (/traded = jumlah market, aktivitas pertama = umur wallet, /value = nilai porto),
dan data market Gamma (tanggal selesai, hasil resolusi).

Setiap INSIDER_SCAN_MINUTES:
1. Ambil trade BUY ≥ INSIDER_MIN_TRADE_USD, gabungkan per (wallet, market, outcome).
2. Kandidat: total ≥ INSIDER_MIN_BET_USD dan harga rata-rata ≤ INSIDER_MAX_PRICE (membeli sisi yang
   dianggap pasar kecil peluangnya). Market olahraga/esports & "Up or Down" dilewati bila
   INSIDER_EXCLUDE_SPORTS (taruhan besar di sana umumnya penjudi, bukan informasi orang dalam).
3. Skor (maks 15): wallet baru, sedikit market, harga longshot, nominal besar, porsi besar dari porto,
   market selesai < 48 jam. Skor ≥ INSIDER_MIN_SCORE → dicatat di insider_flags + alert Telegram.
4. Setelah market resolve: hasil (WIN/LOSS) dicatat → ketepatan sinyal (/insider, dashboard).

Ini heuristik — pola yang sama juga dimiliki penjudi besar atau wallet kedua trader lama. Bukan bukti.
"""
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.models import InsiderFlag

logger = get_logger("insider")

SPORTS_RE = re.compile(
    r"\bvs\.?\b|\bO/U\b|\bspread\b|moneyline|\(BO\d\)|game \d+ winner|map \d+ winner|up or down|"
    r"\bwin on \d{4}-\d{2}-\d{2}\b|total (goals|points|kills)", re.I)
PROFILE_TTL = 6 * 3600
MAX_SCORE = 15
CHECK_INTERVAL = timedelta(minutes=30)
GIVE_UP_AFTER = timedelta(days=60)


def _get(path: str, **params) -> Any:
    from app.paper_trading.wallets import _get as wallets_get
    return wallets_get(path, **params)


def recent_big_trades(min_usd: Optional[float] = None, limit: int = 500) -> List[Dict[str, Any]]:
    """Trade taker terbaru di semua market dengan nominal ≥ min_usd."""
    return _get("/trades", limit=limit, takerOnly="true", filterType="CASH",
                filterAmount=int(min_usd or settings.INSIDER_MIN_TRADE_USD)) or []


def group_bets(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """BUY digabung per (wallet, market, outcome): nominal, shares, harga rata-rata, waktu terakhir."""
    groups: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    for t in trades:
        if str(t.get("side")).upper() != "BUY" or not t.get("proxyWallet") or not t.get("conditionId"):
            continue
        key = (t["proxyWallet"].lower(), t["conditionId"], int(t.get("outcomeIndex") or 0))
        size, price = float(t.get("size") or 0), float(t.get("price") or 0)
        g = groups.setdefault(key, {
            "wallet": key[0], "condition_id": key[1], "outcome_index": key[2], "outcome": t.get("outcome"),
            "title": t.get("title"), "slug": t.get("eventSlug") or t.get("slug"),
            "name": t.get("name") or t.get("pseudonym"), "pseudonym": t.get("pseudonym"),
            "cash": 0.0, "shares": 0.0, "first_ts": int(t.get("timestamp") or 0), "last_ts": 0})
        g["cash"] += size * price
        g["shares"] += size
        g["first_ts"] = min(g["first_ts"], int(t.get("timestamp") or 0))
        g["last_ts"] = max(g["last_ts"], int(t.get("timestamp") or 0))
    for g in groups.values():
        g["avg_price"] = g["cash"] / g["shares"] if g["shares"] else None
    return list(groups.values())


def is_candidate(bet: Dict[str, Any]) -> bool:
    if bet["cash"] < settings.INSIDER_MIN_BET_USD or bet["avg_price"] is None:
        return False
    if bet["avg_price"] > settings.INSIDER_MAX_PRICE:
        return False
    if settings.INSIDER_EXCLUDE_SPORTS and SPORTS_RE.search(str(bet.get("title") or "")):
        return False
    return True


def wallet_profile(address: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """{markets_traded, first_ts, age_days, portfolio_value} — jumlah market & umur di-cache 6 jam."""
    from app.paper_trading.live_market_data import _cached

    now = now or datetime.now(timezone.utc)

    def load():
        traded = (_get("/traded", user=address) or {}).get("traded")
        first = _get("/activity", user=address, limit=1, sortBy="TIMESTAMP", sortDirection="ASC") or []
        return {"markets_traded": int(traded) if traded is not None else None,
                "first_ts": int(first[0].get("timestamp") or 0) if first else None}

    base = _cached(f"insider_profile:{address}", PROFILE_TTL, load)
    try:
        value = float((_get("/value", user=address) or [{}])[0].get("value") or 0)
    except Exception:
        value = None
    age = (now.timestamp() - base["first_ts"]) / 86400 if base.get("first_ts") else None
    return {**base, "age_days": round(age, 1) if age is not None else None, "portfolio_value": value}


def market_info(condition_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """{conditionId: {end, closed, winner, question}} dari Gamma (tanpa menyimpan snapshot)."""
    from app.market_collector.collector import determine_winning_outcome, fetch_markets_by_condition_ids
    from app.paper_trading.wallets import _parse_end

    out = {}
    for m in fetch_markets_by_condition_ids(list(condition_ids)):
        cid = m.get("conditionId")
        if not cid:
            continue
        out[cid] = {"end": _parse_end(m.get("endDate")), "closed": bool(m.get("closed")),
                    "winner": determine_winning_outcome(m), "question": m.get("question")}
    return out


def score_bet(bet: Dict[str, Any], profile: Dict[str, Any], end: Optional[datetime],
              now: datetime) -> Tuple[int, List[str]]:
    """Skor 0–15 dan alasan (bahasa Indonesia)."""
    score, reasons = 0, []
    age = profile.get("age_days")
    if age is not None:
        pts = 3 if age <= 3 else 2 if age <= 14 else 1 if age <= 60 else 0
        if pts:
            score += pts
            reasons.append(f"wallet baru ({age:.0f} hari)" if age >= 1 else "wallet baru (< 1 hari)")
    traded = profile.get("markets_traded")
    if traded is not None:
        pts = 3 if traded <= 3 else 2 if traded <= 10 else 1 if traded <= 30 else 0
        if pts:
            score += pts
            reasons.append(f"baru {traded} market")
    price = bet["avg_price"]
    pts = 3 if price <= 0.15 else 2 if price <= 0.30 else 1 if price <= 0.50 else 0
    if pts:
        score += pts
        reasons.append(f"beli sisi longshot {price * 100:.0f}¢ (pasar menilai peluangnya {price * 100:.0f}%)")
    cash = bet["cash"]
    pts = 3 if cash >= 20000 else 2 if cash >= 10000 else 1 if cash >= 5000 else 0
    if pts:
        score += pts
        reasons.append(f"taruhan besar ${cash:,.0f}")
    value = profile.get("portfolio_value")
    if value:
        share = cash / max(value, cash)
        pts = 2 if share >= 0.5 else 1 if share >= 0.25 else 0
        if pts:
            score += pts
            reasons.append(f"{share * 100:.0f}% dari nilai portonya")
    if end is not None and timedelta(0) <= end - now <= timedelta(hours=48):
        score += 1
        reasons.append(f"market selesai dalam {(end - now).total_seconds() / 3600:.0f} jam")
    return score, reasons


def format_flag_alert(flag: InsiderFlag) -> str:
    from app.paper_trading.wallets import profile_url, short

    name = flag.name if flag.name and not flag.name.lower().startswith("0x") else short(flag.wallet)
    lines = [f"🕵️ WALLET MENCURIGAKAN · skor {flag.score}/{MAX_SCORE} · {name}",
             f"Beli {flag.outcome} — {flag.title}",
             f"${float(flag.cash):,.0f} @ {float(flag.avg_price) * 100:.1f}¢ ({float(flag.shares):,.0f} shares)",
             "Pola: " + " · ".join(json.loads(flag.reasons or "[]"))]
    if flag.slug:
        lines.append(f"https://polymarket.com/event/{flag.slug}")
    lines.append(profile_url(flag.wallet))
    lines.append("Heuristik, bukan bukti: pola yang sama juga dimiliki penjudi besar. Bukan saran finansial.")
    return "\n".join(lines)


def scan(now: Optional[datetime] = None) -> List[str]:
    """Satu siklus: cari taruhan mencurigakan baru, simpan, kirim alert. Kembalikan teks alert terkirim."""
    from app.paper_trading.telegram import send_telegram_message

    now = now or datetime.now(timezone.utc)
    bets = [b for b in group_bets(recent_big_trades()) if is_candidate(b)]
    if not bets:
        return []
    infos = {}
    try:
        infos = market_info([b["condition_id"] for b in bets])
    except Exception as err:
        logger.warning("Gagal mengambil info market insider: %s", err)
    sent: List[str] = []
    db = get_db_session()
    try:
        for bet in bets:
            info = infos.get(bet["condition_id"]) or {}
            if info.get("closed"):
                continue  # market sudah tutup: tidak ada lagi yang bisa diikuti
            existing = (db.query(InsiderFlag).filter_by(wallet=bet["wallet"], condition_id=bet["condition_id"],
                                                        outcome_index=bet["outcome_index"]).first())
            if existing is not None:
                if bet["last_ts"] > (existing.last_trade_ts or 0) and bet["cash"] > float(existing.cash):
                    existing.cash, existing.shares = Decimal(str(round(bet["cash"], 2))), Decimal(str(round(bet["shares"], 4)))
                    existing.avg_price = Decimal(str(round(bet["avg_price"], 4)))
                    existing.last_trade_ts = bet["last_ts"]
                continue
            try:
                profile = wallet_profile(bet["wallet"], now)
            except Exception as err:
                logger.warning("Gagal mengambil profil wallet %s: %s", bet["wallet"], err)
                continue
            score, reasons = score_bet(bet, profile, info.get("end"), now)
            if score < settings.INSIDER_MIN_SCORE:
                continue
            name = bet.get("name")
            if not name or str(name).lower().startswith("0x"):
                name = bet.get("pseudonym")  # nama default "0x…-<angka>" → pakai pseudonym
            flag = InsiderFlag(
                wallet=bet["wallet"], name=str(name)[:255] if name else None,
                condition_id=bet["condition_id"], outcome=str(bet.get("outcome") or "")[:100],
                outcome_index=bet["outcome_index"], title=str(bet.get("title") or "")[:512],
                slug=(bet.get("slug") or None), cash=Decimal(str(round(bet["cash"], 2))),
                shares=Decimal(str(round(bet["shares"], 4))), avg_price=Decimal(str(round(bet["avg_price"], 4))),
                score=score, reasons=json.dumps(reasons), wallet_age_days=profile.get("age_days"),
                markets_traded=profile.get("markets_traded"), market_end=info.get("end"),
                first_trade_ts=bet["first_ts"], last_trade_ts=bet["last_ts"], flagged_at=now)
            db.add(flag)
            db.commit()
            if settings.INSIDER_ALERTS:
                text = format_flag_alert(flag)
                result = send_telegram_message(text, reply_markup={"inline_keyboard": [[
                    {"text": "✅ Ikuti wallet", "callback_data": f"wf:{flag.wallet}"},
                    {"text": "📊 Detail", "callback_data": f"wd:{flag.wallet}"}]]})
                if result.get("success"):
                    flag.alerted_at = now
                    db.commit()
                    sent.append(text)
                else:
                    logger.warning("Alert insider tidak terkirim: %s", result.get("error"))
        return sent
    finally:
        db.close()


def track_results(now: Optional[datetime] = None, limit: int = 60) -> int:
    """Isi hasil (WIN / LOSS / VOID) taruhan yang ditandai setelah market resolve."""
    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    try:
        pending = [f for f in db.query(InsiderFlag).filter(InsiderFlag.result.is_(None)).all()
                   if f.checked_at is None or now - _aware(f.checked_at) >= CHECK_INTERVAL][:limit]
        if not pending:
            return 0
        try:
            infos = market_info([f.condition_id for f in pending])
        except Exception as err:
            logger.warning("Gagal cek resolusi market insider: %s", err)
            return 0
        done = 0
        for f in pending:
            f.checked_at = now
            winner = (infos.get(f.condition_id) or {}).get("winner")
            if winner == "INVALID":
                f.result = "VOID"
            elif winner in ("YES", "NO"):
                index_won = 0 if winner == "YES" else 1
                f.result = "WIN" if f.outcome_index == index_won else "LOSS"
            elif now - _aware(f.flagged_at) > GIVE_UP_AFTER:
                f.result = "UNKNOWN"
            if f.result:
                f.resolved_at = now
                done += 1
        db.commit()
        return done
    finally:
        db.close()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def insider_report(hours: Optional[int] = None, limit: int = 15, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Taruhan yang ditandai + ketepatan sinyal (win rate & ROI seandainya ikut di harga mereka)."""
    now = now or datetime.now(timezone.utc)
    db = get_db_session()
    try:
        q = db.query(InsiderFlag)
        if hours:
            q = q.filter(InsiderFlag.flagged_at >= now - timedelta(hours=hours))
        rows = q.order_by(InsiderFlag.flagged_at.desc()).all()
    finally:
        db.close()
    decided = [f for f in rows if f.result in ("WIN", "LOSS")]
    wins = [f for f in decided if f.result == "WIN"]
    roi = None
    if decided:
        # ROI per $1 seandainya membeli di harga rata-rata mereka: menang → (1 − p)/p, kalah → −1
        roi = sum(((1 - float(f.avg_price)) / float(f.avg_price)) if f.result == "WIN" else -1.0
                  for f in decided) / len(decided)
    by_score = defaultdict(lambda: [0, 0])
    for f in decided:
        bucket = "≥10" if f.score >= 10 else "8–9" if f.score >= 8 else "<8"
        by_score[bucket][0] += 1
        by_score[bucket][1] += 1 if f.result == "WIN" else 0
    items = [{
        "wallet": f.wallet, "name": f.name, "title": f.title, "outcome": f.outcome, "slug": f.slug,
        "cash": float(f.cash), "avg_price": float(f.avg_price), "score": f.score,
        "reasons": json.loads(f.reasons or "[]"), "age_days": float(f.wallet_age_days) if f.wallet_age_days is not None else None,
        "markets_traded": f.markets_traded, "flagged_at": _aware(f.flagged_at).isoformat(),
        "market_end": _aware(f.market_end).isoformat() if f.market_end else None, "result": f.result,
    } for f in rows[:limit]]
    return {"summary": {"flags": len(rows), "decided": len(decided), "wins": len(wins),
                        "win_rate": len(wins) / len(decided) if decided else None, "roi": roi,
                        "avg_price": sum(float(f.avg_price) for f in decided) / len(decided) if decided else None,
                        "pending": sum(1 for f in rows if f.result is None),
                        "by_score": {k: {"n": v[0], "wins": v[1]} for k, v in by_score.items()}},
            "items": items}


def format_insider_report(hours: Optional[int] = None) -> str:
    from app.paper_trading.wallet_bot import md
    from app.paper_trading.wallets import short

    data = insider_report(hours=hours, limit=10)
    s = data["summary"]
    label = f"{hours} jam terakhir" if hours else "semua waktu"
    lines = [f"🕵️ *Insider wallet* ({label})",
             f"Ditandai: {s['flags']} taruhan · selesai {s['decided']} · menunggu {s['pending']}"]
    if s["decided"]:
        lines.append(f"Ketepatan: {s['wins']}/{s['decided']} menang ({s['win_rate'] * 100:.0f}%) dengan harga rata-rata "
                     f"{s['avg_price'] * 100:.0f}¢ · ROI seandainya ikut {s['roi'] * 100:+.0f}%")
        if s["by_score"]:
            lines.append("Per skor: " + " · ".join(f"{k}: {v['wins']}/{v['n']}" for k, v in sorted(s["by_score"].items())))
    icons = {"WIN": "✅", "LOSS": "❌", "VOID": "↩️", "UNKNOWN": "❔", None: "⏳"}
    for it in data["items"]:
        name = it["name"] if it["name"] and not it["name"].lower().startswith("0x") else short(it["wallet"])
        lines += ["", f"{icons.get(it['result'], '•')} *{it['score']}/{MAX_SCORE}* · {md(name)} — {md(it['outcome'])} @ "
                      f"{it['avg_price'] * 100:.0f}¢ · ${it['cash']:,.0f}",
                  f"{md(it['title'])}",
                  f"_{md(' · '.join(it['reasons']))}_"]
    if not data["items"]:
        lines.append("Belum ada taruhan mencurigakan yang tercatat.")
    lines += ["", f"Kriteria: taruhan ≥ ${settings.INSIDER_MIN_BET_USD:,.0f} di harga ≤ {settings.INSIDER_MAX_PRICE * 100:.0f}¢, "
                  f"skor ≥ {settings.INSIDER_MIN_SCORE}. Heuristik, bukan bukti. `/insider 24` untuk 24 jam."]
    return "\n".join(lines)


_last_scan: Dict[str, float] = {"at": 0.0}


def run_insider_scan() -> int:
    """Dipanggil dari loop collector (tiap INSIDER_SCAN_MINUTES); tidak pernah melempar exception."""
    import time as _time

    if not settings.INSIDER_ENABLED:
        return 0
    if _time.monotonic() - _last_scan["at"] < settings.INSIDER_SCAN_MINUTES * 60:
        return 0
    _last_scan["at"] = _time.monotonic()
    sent = 0
    try:
        sent = len(scan())
    except Exception as err:
        logger.error("Scan insider gagal: %s", err, exc_info=True)
    try:
        track_results()
    except Exception as err:
        logger.error("Pelacakan hasil insider gagal: %s", err, exc_info=True)
    return sent
