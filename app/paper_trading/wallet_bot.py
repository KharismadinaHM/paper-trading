"""
Command & tombol Telegram untuk wallet tracker:
  /discover            rekomendasi wallet menarik (tombol ✅ Ikuti / ⏭ Skip)
  /discover refresh    hitung ulang rekomendasi
  /wallets             wallet yang dilacak/diikuti
  /wallet <q>          detail: win rate, PnL, riwayat aktivitas
  /track <alamat>      mulai lacak wallet
  /follow <q>  /unfollow <q>  /skip <q>  /untrack <q>
<q> = alamat 0x…, URL profil, nama wallet, atau nomor dari /discover.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("wallet_bot")

CALLBACK_ACTIONS = {"wf": "follow", "wu": "unfollow", "ws": "skip", "wt": "track", "wd": "detail"}


class BotReply(str):
    """Balasan bot (tetap berupa str) yang bisa membawa tombol inline Telegram."""

    def __new__(cls, text: str, reply_markup: Optional[Dict[str, Any]] = None):
        obj = super().__new__(cls, text)
        obj.reply_markup = reply_markup
        return obj


def md(text: Any) -> str:
    """Escape karakter Markdown (v1) Telegram pada teks dinamis."""
    return "".join("\\" + ch if ch in "_*`[" else ch for ch in str(text if text is not None else ""))


def _label(item: Dict[str, Any]) -> str:
    from app.paper_trading.wallets import short
    return f"{md(item['name'])} ({short(item['address'])})" if item.get("name") else short(item["address"])


def _stats_line(stats: Dict[str, Any]) -> str:
    from app.paper_trading.wallets import ago, money

    wr = f"{stats['win_rate'] * 100:.0f}%" if stats.get("win_rate") is not None else "-"
    parts = [f"WR {wr} ({stats.get('wins', 0)}/{stats.get('resolved', 0)})",
             f"PnL 30h {money(stats.get('pnl'))}"]
    if stats.get("pnl_all") is not None:
        parts.append(f"all-time {money(stats['pnl_all'])}")
    if stats.get("margin") is not None:
        parts.append(f"margin {stats['margin'] * 100:.1f}%")
    if stats.get("avg_entry") is not None:
        parts.append(f"rata-rata beli {stats['avg_entry'] * 100:.0f}¢")
    parts.append(f"posisi terbuka {stats.get('open_positions', 0)}")
    parts.append(f"aktif {ago(stats.get('last_trade_ts'))}")
    return " · ".join(parts)


def _button(text: str, action: str, address: str) -> Dict[str, str]:
    return {"text": text, "callback_data": f"{action}:{address}"}


def build_discover_message(refresh: bool = False) -> BotReply:
    from app.paper_trading import wallets

    if refresh or wallets.candidates_age() is None:
        wallets.discover_wallets()
    candidates = wallets.list_candidates()[:5]
    if not candidates:
        return BotReply("🔎 Belum ada wallet yang memenuhi kriteria (cukup posisi selesai & aktif 7 hari terakhir).\n"
                        "Coba `/discover refresh` nanti.")
    lines = [f"🔎 *Wallet menarik · market {md(settings.WALLET_DISCOVERY_CATEGORY.lower())}*", ""]
    keyboard: List[List[Dict[str, str]]] = []
    for c in candidates:
        lines.append(f"*{c['rank']}. {_label(c)}*")
        lines.append(_stats_line(c["stats"]))
        lines.append(f"Kenapa: {md(c['reason'])}")
        lines.append(wallets.profile_url(c["address"]))
        lines.append("")
        name = (c.get("name") or wallets.short(c["address"]))[:18]
        keyboard.append([_button(f"✅ Ikuti #{c['rank']} {name}", "wf", c["address"]),
                         _button(f"⏭ Skip #{c['rank']}", "ws", c["address"])])
    lines.append("Ikuti = alert real-time setiap wallet ini bertransaksi. Atau ketik `/follow <nomor>` / `/skip <nomor>`.")
    return BotReply("\n".join(lines), {"inline_keyboard": keyboard})


def build_wallets_message() -> BotReply:
    from app.paper_trading import wallets

    tracked = wallets.list_tracked()
    if not tracked:
        return BotReply("👛 Belum ada wallet yang dilacak. Pakai `/discover` atau `/track <alamat 0x…>`.")
    lines = ["👛 *Wallet yang dilacak*", ""]
    keyboard = []
    for i, w in enumerate(tracked, start=1):
        bell = "🔔" if w["follow"] else "🔕"
        lines.append(f"{i}. {bell} {_label(w)}")
        if w["stats"]:
            lines.append(f"   {_stats_line(w['stats'])}")
        keyboard.append([_button(f"📊 {(w.get('name') or wallets.short(w['address']))[:20]}", "wd", w["address"])])
    lines += ["", "🔔 = diikuti (alert transaksi aktif). Detail: `/wallet <nama/alamat>`."]
    return BotReply("\n".join(lines), {"inline_keyboard": keyboard})


def build_wallet_detail(query: str) -> BotReply:
    from app.paper_trading import wallets

    address = wallets.resolve_wallet(query)
    stats = wallets.get_stats(address)
    activity = wallets.recent_activity(address, limit=8)
    tracked = {w["address"]: w for w in wallets.list_tracked(include_skipped=True)}.get(address)
    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    label = settings.NOTIFY_TIMEZONE_LABEL
    status = ("🔔 diikuti" if tracked["follow"] else "🔕 dilacak") if tracked and tracked["status"] == "tracking" \
        else ("⏭ di-skip" if tracked else "belum dilacak")
    lines = [
        f"👛 *{_label({'name': stats.get('name'), 'address': address})}* — {status}",
        _stats_line(stats),
    ]
    if stats.get("weather_share") is not None:
        lines.append(f"Transaksi cuaca {stats['weather_share'] * 100:.0f}% · volume {stats.get('days')} hari "
                     f"{wallets.money(stats.get('volume_month') or stats.get('recent_volume')).lstrip('+')}"
                     f" · nilai portofolio {wallets.money(stats.get('portfolio_value')).lstrip('+')}")
    lines += ["", "*Aktivitas terakhir*"]
    for a in activity:
        at = datetime.fromtimestamp(a["timestamp"], tz)
        price = f" @ {float(a['price']) * 100:.1f}¢" if a.get("price") else ""
        usdc = f" (${float(a['usdc']):,.2f})" if a.get("usdc") else ""
        lines.append(f"• {at:%d %b %H:%M} {label} · {md(a.get('type'))} {md(a.get('side') or '')} "
                     f"{md(a.get('outcome') or '')}{price}{usdc} — {md(a.get('title'))}")
    if not activity:
        lines.append("• (belum ada aktivitas)")
    lines += ["", wallets.profile_url(address)]
    following = bool(tracked and tracked["follow"] and tracked["status"] == "tracking")
    buttons = [_button("🔕 Berhenti ikuti", "wu", address) if following else _button("✅ Ikuti", "wf", address)]
    if not tracked or tracked["status"] != "tracking":
        buttons.append(_button("👁 Lacak saja", "wt", address))
    return BotReply("\n".join(lines), {"inline_keyboard": [buttons]})


def _do(action: str, query: str) -> BotReply:
    from app.paper_trading import wallets

    address = wallets.resolve_wallet(query)
    if action == "follow":
        w = wallets.set_follow(address, True)
        return BotReply(f"✅ Mengikuti {_label(w)}.\n{_stats_line(w['stats']) if w['stats'] else ''}\n"
                        "Alert dikirim setiap wallet ini bertransaksi"
                        + (f" (≥ ${settings.WALLET_ALERT_MIN_USDC:g})." if settings.WALLET_ALERT_MIN_USDC else "."),
                        {"inline_keyboard": [[_button("🔕 Berhenti ikuti", "wu", address)]]})
    if action == "unfollow":
        w = wallets.set_follow(address, False)
        return BotReply(f"🔕 Berhenti mengikuti {_label(w)} (masih dilacak).",
                        {"inline_keyboard": [[_button("✅ Ikuti lagi", "wf", address)]]})
    if action == "skip":
        wallets.skip_wallet(address)
        return BotReply(f"⏭ {wallets.short(address)} di-skip dan tidak akan direkomendasikan lagi.")
    if action == "track":
        w = wallets.track_wallet(address)
        return BotReply(f"👁 Melacak {_label(w)}.\n{_stats_line(w['stats'])}",
                        {"inline_keyboard": [[_button("✅ Ikuti", "wf", address), _button("📊 Detail", "wd", address)]]})
    if action == "untrack":
        removed = wallets.untrack_wallet(address)
        return BotReply(f"🗑 {wallets.short(address)} dihapus dari daftar." if removed
                        else f"{wallets.short(address)} tidak ada di daftar.")
    if action == "detail":
        return build_wallet_detail(address)
    raise ValueError(action)


def handle_wallet_command(cmd: str, args: List[str]) -> Optional[BotReply]:
    """Balasan untuk command wallet, atau None jika bukan command wallet."""
    from app.paper_trading.wallets import WalletError

    query = " ".join(args).strip()
    try:
        if cmd in ("/discover", "/cariwallet"):
            return build_discover_message(refresh=query.lower() == "refresh")
        if cmd == "/wallets":
            return build_wallets_message()
        if cmd in ("/wallet", "/track", "/follow", "/unfollow", "/skip", "/untrack"):
            if not query:
                return BotReply(f"Pakai: `{cmd} <alamat 0x… / nama / nomor dari /discover>`")
            if cmd == "/wallet":
                return build_wallet_detail(query)
            return _do(cmd.lstrip("/"), query)
    except WalletError as err:
        return BotReply(f"⚠️ {md(err)}")
    except Exception as err:
        logger.error("Command wallet %s gagal: %s", cmd, err, exc_info=True)
        return BotReply("⚠️ Gagal mengambil data dari Polymarket. Coba lagi sebentar lagi.")
    return None


def handle_wallet_callback(data: str) -> Optional[BotReply]:
    """Tombol inline 'wf:<alamat>' dst."""
    from app.paper_trading.wallets import WalletError

    prefix, _, address = str(data or "").partition(":")
    action = CALLBACK_ACTIONS.get(prefix)
    if not action or not address:
        return None
    try:
        return _do(action, address)
    except WalletError as err:
        return BotReply(f"⚠️ {md(err)}")
    except Exception as err:
        logger.error("Tombol wallet %s gagal: %s", data, err, exc_info=True)
        return BotReply("⚠️ Gagal memproses. Coba lagi sebentar lagi.")
