"""
Interactive Telegram Bot Polling Service untuk Paper Trading.
Mendengarkan perintah interaktif (/start, /status, /positions, /trades, /performance, /ping)
dan mengirimkan balasan real-time ke pengguna Telegram.
"""
import logging
import os
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

import requests

from app.paper_trading.telegram import send_telegram_message

logger = logging.getLogger("paper_trading.telegram_bot")

# Import paper service functions
try:
    from app.paper_service import (
        get_account_status,
        get_open_positions,
        get_performance,
        get_trade_history,
    )
except ImportError:
    from app.paper_trading.paper_service import (  # type: ignore
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
        "🏓 `/ping` - Tes respon server bot\n"
        "❓ `/help` - Tampilkan panduan ini\n\n"
        "💡 _Notifikasi otomatis sinyal BUY dan Settlement akan dikirim ke chat ini secara real-time._"
    )


def build_status_message() -> str:
    """Format status akun trading."""
    status = get_account_status()
    balance = _fmt_money(status.get("balance", Decimal("0.00")))
    invested = _fmt_money(status.get("invested", Decimal("0.00")))
    realized_pnl = _fmt_pnl(status.get("realized_pnl", Decimal("0.00")))
    win_rate = status.get("win_rate", Decimal("0.00"))
    open_trades = status.get("open_trades", 0)

    try:
        wr_pct = f"{float(win_rate) * 100:.1f}%"
    except Exception:
        wr_pct = f"{win_rate}%"

    return (
        "📊 *Ringkasan Akun Paper Trading*\n"
        "────────────────────\n"
        f"💰 *Saldo*: `{balance}`\n"
        f"🔒 *Terinvestasi*: `{invested}`\n"
        f"📈 *Realized P/L*: `{realized_pnl}`\n"
        f"🎯 *Win Rate*: `{wr_pct}`\n"
        f"📂 *Posisi Aktif*: `{open_trades}` trade\n"
        "────────────────────\n"
        "Gunakan `/positions` untuk melihat posisi aktif."
    )


def build_positions_message() -> str:
    """Format posisi aktif."""
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

        lines.append(
            f"*{i}. {market}*\n"
            f"   • Sisi: `{side}` | Entry: `{entry}`\n"
            f"   • Ukuran: `{size}` ({shares} shares)\n"
            f"   • Harga Saat Ini: `{curr}`\n"
            f"   • Floating P/L: *{u_pnl}*\n"
        )
    return "\n".join(lines)


def build_trades_message(limit: int = 5) -> str:
    """Format riwayat transaksi terakhir."""
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

        lines.append(
            f"{status_icon} *{market}*\n"
            f"   • Entry: `{entry}` → Exit: `{exit_p}`\n"
            f"   • Net P/L: *{net_pnl}* | Waktu: `{date}`\n"
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
