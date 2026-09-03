"""
Modul Telegram Notification untuk Paper Trading.
Menyediakan formatting pesan dan pengiriman ke Telegram Bot API via requests.
"""
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Optional, Union

try:
    import requests
except ImportError:
    requests = None
    import json
    import urllib.request


@dataclass
class PaperBuySignal:
    market: str
    side: str
    entry_price: Union[str, float, Decimal]
    position_size: Union[str, float, Decimal]
    shares: Union[str, float, Decimal]
    expected_peak: str
    reason: str


@dataclass
class PaperTradeSettled:
    entry_price: Union[str, float, Decimal]
    result: str  # WIN / LOSS atau WON / LOST
    gross_pnl: Union[str, float, Decimal]
    fees: Union[str, float, Decimal]
    net_pnl: Union[str, float, Decimal]
    old_balance: Union[str, float, Decimal]
    new_balance: Union[str, float, Decimal]


def _format_money(val: Any) -> str:
    """Format nilai ke string dengan simbol $, hindari duplikasi jika sudah ada $."""
    if val is None:
        return ""
    s = str(val).strip()
    if s.startswith("$"):
        return s
    return f"${s}"


def _format_pnl(val: Any) -> str:
    """Format nilai P/L (mendukung raw Decimal/float atau string seperti +$0.3514)."""
    if val is None:
        return ""
    if isinstance(val, (int, float, Decimal)):
        if val > 0:
            return f"+${val:,.4f}".rstrip("0").rstrip(".")
        elif val < 0:
            return f"-${abs(val):,.4f}".rstrip("0").rstrip(".")
        return f"${val:,.2f}"
    return str(val).strip()


def format_paper_buy(data: Union[Dict[str, Any], PaperBuySignal]) -> str:
    """
    Format pesan Telegram untuk event Paper BUY signal.
    
    Output format:
    🟢 PAPER BUY
    Market: {market}
    Side: {side}
    Entry: ${entry_price}
    Position: ${position_size}
    Shares: {shares}
    Expected Peak: {expected_peak}
    Reason: {reason}
    """
    if isinstance(data, PaperBuySignal):
        d = {
            "market": data.market,
            "side": data.side,
            "entry_price": data.entry_price,
            "position_size": data.position_size,
            "shares": data.shares,
            "expected_peak": data.expected_peak,
            "reason": data.reason,
        }
    else:
        d = data

    entry = _format_money(d.get("entry_price", ""))
    position = _format_money(d.get("position_size", ""))
    shares = str(d.get("shares", ""))
    market = str(d.get("market", ""))
    side = str(d.get("side", ""))
    expected_peak = str(d.get("expected_peak", ""))
    reason = str(d.get("reason", ""))

    return (
        "🟢 PAPER BUY\n"
        f"Market: {market}\n"
        f"Side: {side}\n"
        f"Entry: {entry}\n"
        f"Position: {position}\n"
        f"Shares: {shares}\n"
        f"Expected Peak: {expected_peak}\n"
        f"Reason: {reason}"
    )


def format_paper_settled(data: Union[Dict[str, Any], PaperTradeSettled]) -> str:
    """
    Format pesan Telegram untuk event Paper trade settled.
    
    Output format:
    ✅ PAPER TRADE SETTLED
    Entry: ${entry_price}
    Result: {WIN/LOSS}
    Gross P/L: {gross_pnl}
    Fees: {fees}
    Net P/L: {net_pnl}
    Balance: ${old_balance} → ${new_balance}
    """
    if isinstance(data, PaperTradeSettled):
        d = {
            "entry_price": data.entry_price,
            "result": data.result,
            "gross_pnl": data.gross_pnl,
            "fees": data.fees,
            "net_pnl": data.net_pnl,
            "old_balance": data.old_balance,
            "new_balance": data.new_balance,
        }
    else:
        d = data

    entry = _format_money(d.get("entry_price", ""))
    result = str(d.get("result", "")).upper()
    gross_pnl = _format_pnl(d.get("gross_pnl", ""))
    fees = _format_pnl(d.get("fees", ""))
    net_pnl = _format_pnl(d.get("net_pnl", ""))
    old_bal = _format_money(d.get("old_balance", ""))
    new_bal = _format_money(d.get("new_balance", ""))

    return (
        "✅ PAPER TRADE SETTLED\n"
        f"Entry: {entry}\n"
        f"Result: {result}\n"
        f"Gross P/L: {gross_pnl}\n"
        f"Fees: {fees}\n"
        f"Net P/L: {net_pnl}\n"
        f"Balance: {old_bal} → {new_bal}"
    )


try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def send_telegram_message(
    text: str,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    parse_mode: Optional[str] = None,
    timeout: int = 10,
) -> Dict[str, Any]:
    """
    Mengirim pesan teks ke Telegram Bot API via HTTP POST.
    Jika bot_token atau chat_id tidak disertakan, otomatis membaca dari ENV:
    - TELEGRAM_BOT_TOKEN
    - TELEGRAM_CHAT_ID
    """
    token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
    target_chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")

    if not token or not target_chat_id:
        return {
            "success": False,
            "error": "TELEGRAM_BOT_TOKEN atau TELEGRAM_CHAT_ID belum diatur.",
            "message": text,
        }

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": target_chat_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    # Gunakan library requests jika ada
    if requests is not None:
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            return {"success": True, "response": resp.json()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Fallback ke standard library urllib
    try:
        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=req_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            res_body = json.loads(response.read().decode("utf-8"))
            return {"success": True, "response": res_body}
    except Exception as e:
        return {"success": False, "error": str(e)}


def notify_paper_buy(
    data: Union[Dict[str, Any], PaperBuySignal],
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Shortcut: Format pesan Paper BUY lalu kirim ke Telegram."""
    msg = format_paper_buy(data)
    return send_telegram_message(msg, bot_token=bot_token, chat_id=chat_id)


def notify_paper_settled(
    data: Union[Dict[str, Any], PaperTradeSettled],
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Shortcut: Format pesan Paper Trade Settled lalu kirim ke Telegram."""
    msg = format_paper_settled(data)
    return send_telegram_message(msg, bot_token=bot_token, chat_id=chat_id)
