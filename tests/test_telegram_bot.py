import unittest
from unittest.mock import MagicMock, patch
from decimal import Decimal

from app.paper_trading.telegram_bot import (
    handle_incoming_message,
    build_help_message,
    build_status_message,
    build_positions_message,
    build_trades_message,
    build_performance_message,
)


class TestTelegramBotCommands(unittest.TestCase):

    def test_help_message(self):
        msg = build_help_message()
        self.assertIn("/status", msg)
        self.assertIn("/positions", msg)
        self.assertIn("/trades", msg)
        self.assertIn("/performance", msg)

    def test_handle_start_and_help(self):
        reply_start = handle_incoming_message("/start", sender_chat_id="123", allowed_chat_id="123")
        self.assertIn("Polymarket Weather Paper Trading Bot", reply_start)

        reply_help = handle_incoming_message("/help", sender_chat_id="123", allowed_chat_id="123")
        self.assertIn("/status", reply_help)

    def test_handle_status(self):
        reply = handle_incoming_message("/status", sender_chat_id="123", allowed_chat_id="123")
        self.assertIn("Ringkasan Akun Paper Trading", reply)
        self.assertIn("Saldo", reply)

    def test_handle_positions(self):
        reply = handle_incoming_message("/positions", sender_chat_id="123", allowed_chat_id="123")
        self.assertIn("Posisi Terbuka", reply)

    def test_handle_trades(self):
        reply = handle_incoming_message("/trades", sender_chat_id="123", allowed_chat_id="123")
        self.assertIn("Riwayat Transaksi", reply)

    def test_handle_performance(self):
        reply = handle_incoming_message("/performance", sender_chat_id="123", allowed_chat_id="123")
        self.assertIn("Metrik Performa Trading", reply)

    def test_handle_ping(self):
        reply = handle_incoming_message("/ping", sender_chat_id="123", allowed_chat_id="123")
        self.assertIn("Pong!", reply)

    def test_unauthorized_chat(self):
        reply = handle_incoming_message("/status", sender_chat_id="999", allowed_chat_id="123")
        self.assertIn("Akses Ditolak", reply)

    def test_non_command(self):
        reply = handle_incoming_message("halo halo apa kabar", sender_chat_id="123", allowed_chat_id="123")
        self.assertIsNone(reply)

    def test_unknown_command(self):
        reply = handle_incoming_message("/foobar", sender_chat_id="123", allowed_chat_id="123")
        self.assertIn("tidak dikenal", reply)

    def test_positions_contains_polymarket_links_and_mtm(self):
        reply = handle_incoming_message("/positions", sender_chat_id="123", allowed_chat_id="123")
        self.assertIn("Posisi Terbuka", reply)
        self.assertIn("[Buka di Polymarket](https://polymarket.com/markets?_q=", reply)
        self.assertIn("Avg → Now", reply)
        self.assertIn("Value:", reply)
        self.assertIn("To Win:", reply)

    def test_status_contains_portfolio_mtm(self):
        reply = handle_incoming_message("/status", sender_chat_id="123", allowed_chat_id="123")
        self.assertIn("Portfolio (MTM)", reply)
        self.assertIn("Saldo Kas (Cash)", reply)
        self.assertIn("Floating P/L", reply)

    def test_trades_contains_polymarket_links(self):
        reply = handle_incoming_message("/trades", sender_chat_id="123", allowed_chat_id="123")
        self.assertIn("Riwayat Transaksi", reply)
        self.assertIn("[Buka di Polymarket](https://polymarket.com/markets?_q=", reply)


if __name__ == "__main__":
    unittest.main()
