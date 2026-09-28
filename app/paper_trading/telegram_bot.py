"""
Interactive Telegram Bot Polling Service untuk Paper Trading.
Mendengarkan perintah interaktif (/start, /status, /positions, /trades, /performance, /ping)
dan mengirimkan balasan real-time ke pengguna Telegram.
"""
import logging
import os
import time
import urllib.parse
from decimal import Decimal
from typing import Any, Dict, List, Optional

import requests

from app.paper_trading.telegram import send_telegram_message

logger = logging.getLogger("paper_trading.telegram_bot")

# Import paper service functions
from app.paper_service import (
    get_account_status,
    get_open_positions,
    get_performance,
    get_trade_history,
)


def _fmt_money(val: Any) -> str:
    """Helper format uang dengan simbol $."""
    if val is None:
        return "$0.00"
    if isinstance(val, (int, float, Decimal)):
        return f"${val:,.2f}"
    s = str(val).strip()
    return s if s.startswith("$") else f"${s}"


def _fmt_pnl(val: Any) -> str:
    """Helper format PnL (+/-)."""
    if val is None:
        return "$0.00"
    try:
        dec = Decimal(str(val).replace("$", "").replace("+", "").strip())
        if dec > 0:
            return f"+${dec:,.2f}"
        elif dec < 0:
            return f"-${abs(dec):,.2f}"
        return f"${dec:,.2f}"
    except Exception:
        return str(val)


def build_help_message() -> str:
    """Pesan bantuan dan daftar perintah."""
    return (
        "🤖 *Polymarket Weather Paper Trading Bot*\n\n"
        "Gunakan perintah berikut untuk memantau aktivitas trading Anda:\n\n"
        "📊 `/status` - Cek saldo, realized P/L, & win rate\n"
        "📈 `/positions` - Daftar posisi trading aktif\n"
        "📜 `/trades` - Riwayat 5 transaksi terakhir yang selesai\n"
        "🏆 `/performance` - Ringkasan metrik performa & drawdown\n"
        "🌡️ `/rekomendasi` - Kota yang sedang menjelang jam puncak suhu\n"
        "🎯 `/statistik` - Win rate saran beli bot (`/statistik 7` untuk 7 hari terakhir)\n"
        "🔥 `/volume` - 7 kota dengan volume market cuaca terbesar (`/volume 10` untuk 10 kota)\n"
        "🏓 `/ping` - Tes respon server bot\n"
        "❓ `/help` - Tampilkan panduan ini\n\n"
        "💡 _Notifikasi otomatis sinyal BUY, rekomendasi jam puncak, dan Settlement dikirim ke chat ini secara real-time._"
    )


def build_status_message() -> str:
    """Format status akun trading."""
    status = get_account_status()
    balance = _fmt_money(status.get("balance", Decimal("0.00")))
    portfolio_val = _fmt_money(status.get("portfolio_value", status.get("balance", Decimal("0.00"))))
    invested = _fmt_money(status.get("invested", Decimal("0.00")))
    realized_pnl = _fmt_pnl(status.get("realized_pnl", Decimal("0.00")))
    unrealized_pnl = _fmt_pnl(status.get("unrealized_pnl", Decimal("0.00")))
    total_pnl = _fmt_pnl(status.get("total_pnl", Decimal("0.00")))
    win_rate = status.get("win_rate", Decimal("0.00"))
    open_trades = status.get("open_trades", 0)

    try:
        wr_pct = f"{float(win_rate) * 100:.1f}%"
    except Exception:
        wr_pct = f"{win_rate}%"

    return (
        "📊 *Ringkasan Akun Paper Trading*\n"
        "────────────────────\n"
        f"💼 *Portfolio (MTM)*: `{portfolio_val}`\n"
        f"💰 *Saldo Kas (Cash)*: `{balance}`\n"
        f"🔒 *Terinvestasi*: `{invested}`\n"
        f"⏳ *Floating P/L*: `{unrealized_pnl}`\n"
        f"📈 *Realized P/L*: `{realized_pnl}`\n"
        f"💎 *Total P/L*: `{total_pnl}`\n"
        f"🎯 *Win Rate*: `{wr_pct}`\n"
        f"📂 *Posisi Aktif*: `{open_trades}` trade\n"
        "────────────────────\n"
        "Gunakan `/positions` untuk melihat posisi aktif."
    )


def build_positions_message() -> str:
    """Format posisi aktif dengan link Polymarket dan metrik dinamis MTM."""
    positions = get_open_positions()
    if not positions:
        return "📈 *Posisi Terbuka*\n\nTidak ada posisi terbuka saat ini."

    lines = [f"📈 *Posisi Terbuka ({len(positions)})*\n────────────────────"]
    for i, pos in enumerate(positions, 1):
        market = pos.get("market", "N/A")
        side = pos.get("side", "BUY")
        entry = _fmt_money(pos.get("entry_price", 0))
        size = _fmt_money(pos.get("size", 0))
        shares = pos.get("shares", 0)
        curr = _fmt_money(pos.get("current_price", 0))
        u_pnl = _fmt_pnl(pos.get("unrealized_pnl", 0))
        current_val = _fmt_money(pos.get("current_value", pos.get("size", 0)))
        to_win = _fmt_money(pos.get("to_win", pos.get("shares", 0)))
        avg_to_now = pos.get("avg_to_now") or f"{entry} → {curr}"
        roi_pct = pos.get("roi_pct", Decimal("0.00"))
        poly_url = pos.get("polymarket_url") or f"https://polymarket.com/markets?_q={urllib.parse.quote(str(market))}"

        lines.append(
            f"*{i}. {market}*\n"
            f"   • Sisi: `{side}` | Shares: `{shares}`\n"
            f"   • Avg → Now: `{avg_to_now}`\n"
            f"   • Traded: `{size}` | To Win: `{to_win}`\n"
            f"   • Value: `{current_val}` | P/L: *{u_pnl}* ({float(roi_pct):+.1f}%)\n"
            f"   🌐 [Buka di Polymarket]({poly_url})\n"
        )
    return "\n".join(lines)


def build_trades_message(limit: int = 5) -> str:
    """Format riwayat transaksi terakhir dengan link Polymarket."""
    trades = get_trade_history(limit=limit)
    if not trades:
        return "📜 *Riwayat Transaksi*\n\nBelum ada transaksi yang selesai."

    lines = [f"📜 *Riwayat Transaksi ({len(trades)} Terakhir)*\n────────────────────"]
    for t in trades:
        status_icon = "🟢 [WON]" if str(t.get("status", "")).upper() == "WON" else "🔴 [LOST]"
        market = t.get("market", "N/A")
        entry = _fmt_money(t.get("entry_price", 0))
        exit_p = _fmt_money(t.get("exit_price", 0))
        net_pnl = _fmt_pnl(t.get("net_pnl", 0))
        date = t.get("date", "")
        poly_url = t.get("polymarket_url") or f"https://polymarket.com/markets?_q={urllib.parse.quote(str(market))}"

        lines.append(
            f"{status_icon} *{market}*\n"
            f"   • Entry: `{entry}` → Exit: `{exit_p}`\n"
            f"   • Net P/L: *{net_pnl}* | Waktu: `{date}`\n"
            f"   🌐 [Buka di Polymarket]({poly_url})\n"
        )
    return "\n".join(lines)


def build_performance_message(strategy: Optional[str] = None) -> str:
    """Format metrik performa sistem."""
    perf = get_performance(strategy_version=strategy)
    trades = perf.get("trades", 0)
    wins = perf.get("wins", 0)
    losses = perf.get("losses", 0)
    win_rate = perf.get("win_rate", Decimal("0.00"))
    roi = perf.get("roi", Decimal("0.00"))
    r_pnl = _fmt_pnl(perf.get("realized_pnl", 0))
    u_pnl = _fmt_pnl(perf.get("unrealized_pnl", 0))
    max_dd = perf.get("max_drawdown", Decimal("0.00"))

    try:
        wr_pct = f"{float(win_rate) * 100:.1f}%"
    except Exception:
        wr_pct = f"{win_rate}%"

    try:
        roi_pct = f"{float(roi) * 100:+.2f}%"
    except Exception:
        roi_pct = f"{roi}%"

    try:
        dd_pct = f"{float(max_dd) * 100:.2f}%"
    except Exception:
        dd_pct = f"{max_dd}%"

    strat_title = f" ({strategy})" if strategy else ""
    return (
        f"🏆 *Metrik Performa Trading{strat_title}*\n"
        "────────────────────\n"
        f"📊 *Total Trades*: `{trades}` (`{wins}` Menang / `{losses}` Kalah)\n"
        f"🎯 *Win Rate*: `{wr_pct}`\n"
        f"📈 *ROI*: `{roi_pct}`\n"
        f"💵 *Realized P/L*: `{r_pnl}`\n"
        f"⏳ *Unrealized P/L*: `{u_pnl}`\n"
        f"📉 *Max Drawdown*: `{dd_pct}`\n"
        "────────────────────"
    )


def build_recommendations_message() -> str:
    """Daftar rekomendasi aktif; jika kosong, tampilkan jendela berikutnya (jam dalam WIB)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.core.config import settings
    from app.paper_service import get_market_suggestions, get_recommendation_schedule
    from app.paper_trading.recommendation_alerts import build_recommendation_message, top_volume_events

    events = top_volume_events([e for e in get_market_suggestions() if e.get("markets")])
    if events:
        return build_recommendation_message(events, title="🌡️ Rekomendasi aktif")

    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    lines = ["🌡️ Belum ada kota yang sedang menjelang jam puncak suhu.", "", "Jendela berikutnya:"]
    schedule = get_recommendation_schedule(limit=200)
    cities = {w["city"] for w in top_volume_events(schedule)}  # hanya kota bervolume besar
    for w in [w for w in schedule if w["city"] in cities][:5]:
        start = datetime.fromisoformat(w["starts_at"]).astimezone(tz)
        kind = "tertinggi" if w["kind"] == "highest" else "terendah"
        lines.append(f"• {w['city']} ({kind}) mulai {start:%H:%M} {settings.NOTIFY_TIMEZONE_LABEL} — {w['starts_in']}")
    if len(lines) == 3:
        lines.append("• (belum ada data market suhu)")
    return "\n".join(lines)


def build_volume_message(limit: Optional[int] = None) -> str:
    """Top kota menurut total volume market suhu open, beserta jam beli & puncak berikutnya (WIB)."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from app.core.config import settings
    from app.paper_service import get_city_volume_summary
    from app.paper_trading.recommendation_alerts import _format_volume, city_hashtag
    from app.paper_trading.weather_peaks import next_recommendation_window

    limit = limit or settings.TELEGRAM_RECOMMENDATION_TOP_CITIES or 7
    rows = get_city_volume_summary(limit=limit)
    if not rows:
        return ("🔥 Belum ada data volume market cuaca.\n\n"
                "Data volume diisi oleh Market Collector; coba lagi setelah satu siklus (±5 menit).")

    now = datetime.now(timezone.utc)
    tz, label = ZoneInfo(settings.NOTIFY_TIMEZONE), settings.NOTIFY_TIMEZONE_LABEL
    lines = [f"🔥 *Top {len(rows)} Volume · Market Cuaca*", ""]
    for i, r in enumerate(rows, start=1):
        lines.append(f"{i}. {city_hashtag(r['city'])} — {_format_volume(r['volume'])} "
                     f"(max {_format_volume(r['highest'])} · min {_format_volume(r['lowest'])})")
        for kind, name in (("highest", "Max"), ("lowest", "Min")):
            w = next_recommendation_window(r["city"], kind, now=now)
            if w is None:
                continue
            start, peak = w.start.astimezone(tz), w.peak_start.astimezone(tz)
            day = "" if peak.date() == now.astimezone(tz).date() else f" ({peak:%d %b})"
            status = "🟢 sedang jam beli" if w.contains(now) else f"beli {start:%H:%M}"
            lines.append(f"   {name}: {status} · puncak {peak:%H:%M} {label}{day}")
    lines += ["", "Volume = total volume semua market suhu yang masih buka di kota tersebut."]
    return "\n".join(lines)


def build_stats_message(days: Optional[int] = None) -> str:
    """Win rate, odds rata-rata, dan ROI saran beli bot, plus hasil terakhir."""
    from app.paper_trading.recommendation_alerts import _format_odd, city_hashtag
    from app.paper_trading.recommendation_results import get_recommendation_stats

    s = get_recommendation_stats(days=days)
    period = f"{days} hari terakhir" if days else "semua waktu"
    if not s["sent"]:
        return f"🎯 *Statistik Saran Bot* ({period})\n\nBelum ada saran yang terkirim."

    def pct(x):
        return f"{x * 100:.0f}%" if x is not None else "-"

    def roi(x):
        return f"{x * 100:+.0f}%" if x is not None else "-"

    lines = [
        f"🎯 *Statistik Saran Bot* ({period})",
        "",
        f"Saran terkirim: {s['sent']} · sudah ada hasil: {s['decided']} · menunggu: {s['pending']}"
        + (f" · batal: {s['void']}" if s["void"] else ""),
        f"✅ Menang {s['wins']} · ❌ Kalah {s['losses']} · *Win rate {pct(s['win_rate'])}*",
        f"Odds rata-rata: {_format_odd(s['avg_odds'])} · ROI per $1: {roi(s['roi'])}",
    ]
    for kind, name in (("highest", "Suhu tertinggi"), ("lowest", "Suhu terendah")):
        k = s["by_kind"][kind]
        if k["decided"]:
            lines.append(f"   {name}: {k['wins']}/{k['decided']} ({pct(k['win_rate'])}) · ROI {roi(k['roi'])}")
    if s["winner_in_alternatives"]:
        lines.append(f"Saat kalah, {s['winner_in_alternatives']}× pemenangnya ada di bracket Alternatif.")
    if s["recent"]:
        lines += ["", "Hasil terakhir:"]
        icons = {"WIN": "✅", "LOSS": "❌", "VOID": "⚪"}
        for r in s["recent"]:
            kind = "max" if r["kind"] == "highest" else "min"
            winner = (f" → menang: {r['winning_bracket']}"
                      if r["result"] == "LOSS" and r["winning_bracket"] else "")
            lines.append(f"{icons.get(r['result'], '')} {city_hashtag(r['city'])} {kind} {r['local_date'][5:]} · "
                         f"{r['bracket'] or '-'} @ {_format_odd(r['price_yes'])}{winner}")
    lines += ["", "Win rate hanya dari saran utama (bracket peluang tertinggi). "
              "ROI = seandainya beli $1 YES di odds saat saran dikirim."]
    return "\n".join(lines)


def handle_incoming_message(text: str, sender_chat_id: str, allowed_chat_id: Optional[str] = None) -> Optional[str]:
    """
    Memproses teks perintah dari pengguna dan menghasilkan respon balasan.
    """
    raw = text.strip()
    if not raw.startswith("/"):
        return None

    # Normalisasi perintah (menghapus @username_bot jika ada di grup, misal /status@MyBot)
    parts = raw.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]

    # Keamanan opsional: batasi hanya chat_id yang diizinkan jika dikonfigurasi
    if allowed_chat_id:
        norm_sender = str(sender_chat_id).strip()
        norm_allowed = str(allowed_chat_id).strip()
        if norm_sender != norm_allowed:
            logger.warning("Pesan ditolak dari unauthorized chat_id: %s", sender_chat_id)
            return (
                "⛔ *Akses Ditolak*\n"
                "Akun atau grup Telegram Anda tidak terdaftar sebagai pengelola bot ini."
            )

    if cmd in ("/start", "/help"):
        return build_help_message()
    elif cmd == "/status":
        return build_status_message()
    elif cmd == "/positions":
        return build_positions_message()
    elif cmd == "/trades":
        limit = 5
        if args and args[0].isdigit():
            limit = min(int(args[0]), 20)
        return build_trades_message(limit=limit)
    elif cmd == "/performance":
        strat = args[0] if args else None
        return build_performance_message(strategy=strat)
    elif cmd in ("/rekomendasi", "/recommendations"):
        return build_recommendations_message()
    elif cmd in ("/statistik", "/stats", "/winrate"):
        days = int(args[0]) if args and args[0].isdigit() and int(args[0]) > 0 else None
        return build_stats_message(days=days)
    elif cmd in ("/volume", "/topvolume"):
        limit = min(int(args[0]), 20) if args and args[0].isdigit() and int(args[0]) > 0 else None
        return build_volume_message(limit=limit)
    elif cmd == "/ping":
        return "🏓 *Pong!*\nSistem Paper Trading aktif dan terhubung."
    else:
        return (
            f"❓ Perintah `{cmd}` tidak dikenal.\n\n"
            "Ketik `/help` untuk melihat daftar perintah yang tersedia."
        )


def start_bot_polling(
    bot_token: Optional[str] = None,
    allowed_chat_id: Optional[str] = None,
    poll_timeout: int = 25,
) -> None:
    """
    Menjalankan loop Long-Polling untuk mendengarkan pesan masuk dari Telegram Bot API.
    """
    token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
    target_chat_id = allowed_chat_id or os.getenv("TELEGRAM_CHAT_ID")

    if not token:
        try:
            from app.core.config import settings
            token = token or getattr(settings, "TELEGRAM_BOT_TOKEN", None)
            target_chat_id = target_chat_id or getattr(settings, "TELEGRAM_CHAT_ID", None)
        except Exception:
            pass

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN belum diset di .env atau environment!")

    logger.info("Memulai Telegram Bot Polling listener...")
    print(f"🤖 Telegram Bot Polling aktif!")
    if target_chat_id:
        print(f"🔒 Terkunci untuk Chat ID: {target_chat_id}")
    print("Tekan Ctrl+C untuk menghentikan.\n")

    offset = 0
    base_url = f"https://api.telegram.org/bot{token}"

    while True:
        try:
            url = f"{base_url}/getUpdates"
            params = {
                "offset": offset,
                "timeout": poll_timeout,
                "allowed_updates": ["message"],
            }
            resp = requests.get(url, params=params, timeout=poll_timeout + 5)
            if resp.status_code != 200:
                logger.error("Error getUpdates: HTTP %s - %s", resp.status_code, resp.text)
                time.sleep(3)
                continue

            data = resp.json()
            if not data.get("ok"):
                logger.error("Telegram API error: %s", data.get("description"))
                time.sleep(3)
                continue

            updates = data.get("result", [])
            for update in updates:
                update_id = update["update_id"]
                offset = update_id + 1

                msg = update.get("message")
                if not msg:
                    continue

                text = msg.get("text")
                chat = msg.get("chat", {})
                chat_id = str(chat.get("id"))

                if not text:
                    continue

                reply = handle_incoming_message(text, sender_chat_id=chat_id, allowed_chat_id=target_chat_id)
                if reply:
                    send_telegram_message(
                        text=reply,
                        bot_token=token,
                        chat_id=chat_id,
                        parse_mode="Markdown",
                    )

        except requests.exceptions.RequestException as e:
            logger.warning("Jaringan bermasalah saat getUpdates: %s", e)
            time.sleep(3)
        except KeyboardInterrupt:
            print("\n🛑 Telegram Bot Polling dihentikan oleh pengguna.")
            break
        except Exception as e:
            logger.exception("Terjadi error tak terduga pada loop bot: %s", e)
            time.sleep(3)
