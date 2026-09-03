import unittest
from unittest.mock import MagicMock, patch
from decimal import Decimal

from app.paper_trading.telegram import (
    PaperBuySignal,
    PaperTradeSettled,
    format_paper_buy,
    format_paper_settled,
    send_telegram_message,
    notify_paper_buy,
    notify_paper_settled,
)


class TestTelegramNotification(unittest.TestCase):

    def test_format_paper_buy_dict(self):
        data = {
            "market": "Temperature in Tokyo",
            "side": "YES",
            "entry_price": "0.74",
            "position_size": "1.00",
            "shares": "1.3514",
            "expected_peak": "14:00",
            "reason": "Weather conditions match strategy criteria.",
        }
        msg = format_paper_buy(data)
        expected = (
            "🟢 PAPER BUY\n"
            "Market: Temperature in Tokyo\n"
            "Side: YES\n"
            "Entry: $0.74\n"
            "Position: $1.00\n"
            "Shares: 1.3514\n"
            "Expected Peak: 14:00\n"
            "Reason: Weather conditions match strategy criteria."
        )
        self.assertEqual(msg, expected)

    def test_format_paper_buy_dataclass(self):
        signal = PaperBuySignal(
            market="Temperature in Tokyo",
            side="YES",
            entry_price=Decimal("0.74"),
            position_size=Decimal("1.00"),
            shares=Decimal("1.3514"),
            expected_peak="14:00",
            reason="Weather conditions match strategy criteria.",
        )
        msg = format_paper_buy(signal)
        self.assertIn("🟢 PAPER BUY", msg)
        self.assertIn("Market: Temperature in Tokyo", msg)
        self.assertIn("Side: YES", msg)
        self.assertIn("Entry: $0.74", msg)
        self.assertIn("Position: $1.00", msg)
        self.assertIn("Shares: 1.3514", msg)
        self.assertIn("Expected Peak: 14:00", msg)
        self.assertIn("Reason: Weather conditions match strategy criteria.", msg)

    def test_format_paper_settled_dict(self):
        data = {
            "entry_price": "0.74",
            "result": "WIN",
            "gross_pnl": "+$0.3514",
            "fees": "-$0.01",
            "net_pnl": "+$0.3414",
            "old_balance": "20.00",
            "new_balance": "20.34",
        }
        msg = format_paper_settled(data)
        expected = (
            "✅ PAPER TRADE SETTLED\n"
            "Entry: $0.74\n"
            "Result: WIN\n"
            "Gross P/L: +$0.3514\n"
            "Fees: -$0.01\n"
            "Net P/L: +$0.3414\n"
            "Balance: $20.00 → $20.34"
        )
        self.assertEqual(msg, expected)

    def test_format_paper_settled_dataclass(self):
        settled = PaperTradeSettled(
            entry_price=Decimal("0.50"),
            result="LOSS",
            gross_pnl=Decimal("-1.00"),
            fees=Decimal("0.00"),
            net_pnl=Decimal("-1.00"),
            old_balance=Decimal("20.00"),
            new_balance=Decimal("19.00"),
        )
        msg = format_paper_settled(settled)
        self.assertIn("✅ PAPER TRADE SETTLED", msg)
        self.assertIn("Entry: $0.50", msg)
        self.assertIn("Result: LOSS", msg)
        self.assertIn("Gross P/L: -$1", msg)
        self.assertIn("Net P/L: -$1", msg)
        self.assertIn("Balance: $20.00 → $19.00", msg)

    @patch("requests.post")
    def test_send_telegram_message_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "result": {"message_id": 123}}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        res = send_telegram_message("Hello Test", bot_token="TOKEN123", chat_id="CHAT456")
        self.assertTrue(res["success"])
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://api.telegram.org/botTOKEN123/sendMessage")
        self.assertEqual(kwargs["json"], {"chat_id": "CHAT456", "text": "Hello Test"})

    def test_send_telegram_missing_env(self):
        with patch.dict("os.environ", {}, clear=True):
            res = send_telegram_message("Test message")
            self.assertFalse(res["success"])
            self.assertIn("belum diatur", res["error"])


if __name__ == "__main__":
    unittest.main()
