import logging
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.core.logging import get_logger, setup_logging


class TestConfigAndLogging(unittest.TestCase):

    def test_config_defaults(self):
        config = Settings()
        self.assertIsInstance(config.SLIPPAGE_BPS, int)
        self.assertIsInstance(config.MAX_POSITION_SIZE, Decimal)
        self.assertIsInstance(config.INITIAL_BALANCE, Decimal)
        self.assertIn("postgresql", config.DATABASE_URL)
        self.assertEqual(config.SLIPPAGE_BPS, 0)
        self.assertEqual(config.MAX_POSITION_SIZE, Decimal("1.00"))
        self.assertEqual(config.INITIAL_BALANCE, Decimal("20.00"))

    def test_config_env_override(self):
        with patch.dict(

            os.environ,
            {
                "SLIPPAGE_BPS": "25",
                "MAX_POSITION_SIZE": "2.50",
                "INITIAL_BALANCE": "50.00",
                "DATABASE_URL": "postgresql://test_user:test_pass@localhost:5433/test_db",
                "TELEGRAM_BOT_TOKEN": "bot12345",
            },
        ):
            config = Settings()
            self.assertEqual(config.SLIPPAGE_BPS, 25)
            self.assertEqual(config.MAX_POSITION_SIZE, Decimal("2.50"))
            self.assertEqual(config.INITIAL_BALANCE, Decimal("50.00"))
            self.assertEqual(config.TELEGRAM_BOT_TOKEN, "bot12345")
            self.assertIn("5433", config.DATABASE_URL)

    def test_logging_file_and_console(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_log_file = Path(tmpdir) / "subfolder" / "test_app.log"
            
            # Setup logging to temporary file
            setup_logging(log_level="DEBUG", log_file=str(test_log_file))
            
            logger = get_logger("test_module")
            logger.info("This is a test information message.")
            logger.warning("This is a test warning message.")

            # Flush handlers
            for h in logging.getLogger().handlers:
                h.flush()

            # Pastikan file log otomatis dibuat
            self.assertTrue(test_log_file.exists())

            # Baca isi file log
            content = test_log_file.read_text(encoding="utf-8")
            
            # Verifikasi format: timestamp | level | module | message
            self.assertIn("INFO", content)
            self.assertIn("test_module", content)
            self.assertIn("This is a test information message.", content)
            self.assertIn("WARNING", content)
            self.assertIn("This is a test warning message.", content)


if __name__ == "__main__":
    unittest.main()
