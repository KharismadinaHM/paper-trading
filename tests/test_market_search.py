import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from app.paper_trading.models import MarketSnapshot
from app.paper_trading.suggestions import search_markets
from app.dashboard import search_markets_api
from app.paper_service import search_market_snapshots


class TestMarketSearch(unittest.TestCase):

    def setUp(self):
        self.now = datetime(2026, 9, 4, 10, 0, 0, tzinfo=timezone.utc)

        self.mock_markets = [
            {
                "market_id": "mkt-nyc-85f",
                "market_name": "Will NYC exceed 85°F on Sep 5?",
                "category": "Temperature",
                "status": "open",
                "resolution_time": self.now + timedelta(hours=3),
                "price_yes": Decimal("0.72"),
                "price_no": Decimal("0.28"),
                "current_price": Decimal("0.72"),
            },
            {
                "market_id": "mkt-austin-95f",
                "market_name": "Will Austin high > 95°F on Sep 5?",
                "category": "Temperature",
                "status": "open",
                "resolution_time": self.now + timedelta(hours=2),
                "price_yes": Decimal("0.26"),
                "price_no": Decimal("0.74"),
                "current_price": Decimal("0.74"),
            },
            {
                "market_id": "mkt-miami-rain",
                "market_name": "Will Miami rain > 0.5 in on Sep 6?",
                "category": "Precipitation",
                "status": "open",
                "resolution_time": self.now + timedelta(hours=18),
                "price_yes": Decimal("0.73"),
                "price_no": Decimal("0.27"),
                "current_price": Decimal("0.73"),
            },
            {
                "market_id": "mkt-chicago-wind",
                "market_name": "Will Chicago wind > 25mph on Sep 5?",
                "category": "Wind",
                "status": "open",
                "resolution_time": self.now + timedelta(hours=5),
                "price_yes": Decimal("0.45"),
                "price_no": Decimal("0.55"),
                "current_price": Decimal("0.45"),
            },
            {
                "market_id": "mkt-denver-snow",
                "market_name": "Will Denver snow > 1 in on Sep 5?",
                "category": "Snow",
                "status": "open",
                "resolution_time": self.now + timedelta(hours=4),
                "price_yes": Decimal("0.75"),
                "price_no": Decimal("0.25"),
                "current_price": Decimal("0.75"),
            },
        ]

    def test_keyword_match_case_insensitive_name(self):
        """Pencarian keyword case-insensitive pada nama pasar."""
        # Cari "nyc" huruf kecil
        results_nyc = search_markets(self.mock_markets, query="nyc", now=self.now)
        self.assertEqual(len(results_nyc), 1)
        self.assertEqual(results_nyc[0]["market_id"], "mkt-nyc-85f")

        # Cari "RAIN" huruf besar
        results_rain = search_markets(self.mock_markets, query="RAIN", now=self.now)
        self.assertEqual(len(results_rain), 1)
        self.assertEqual(results_rain[0]["market_id"], "mkt-miami-rain")

    def test_keyword_match_category(self):
        """Pencarian keyword yang cocok dengan nama kategori."""
        results = search_markets(self.mock_markets, query="wind", now=self.now)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["market_id"], "mkt-chicago-wind")
        self.assertEqual(results[0]["category"], "Wind")

    def test_category_filter(self):
        """Filter eksplisit berdasarkan kategori."""
        results_temp = search_markets(self.mock_markets, category="Temperature", now=self.now)
        self.assertEqual(len(results_temp), 2)
        ids = [m["market_id"] for m in results_temp]
        self.assertIn("mkt-nyc-85f", ids)
        self.assertIn("mkt-austin-95f", ids)

    def test_price_range_filter(self):
        """Filter berdasarkan rentang harga min dan max."""
        # Harga antara 0.40 dan 0.60 -> mkt-chicago-wind (0.45 / 0.55)
        results = search_markets(
            self.mock_markets,
            min_price=0.40,
            max_price=0.60,
            now=self.now,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["market_id"], "mkt-chicago-wind")

    def test_combined_search_and_filters(self):
        """Kombinasi keyword + kategori + price range."""
        results = search_markets(
            self.mock_markets,
            query="sep 5",
            category="Temperature",
            min_price=0.70,
            max_price=0.75,
            now=self.now,
        )
        # Austin dan NYC keduanya Sep 5, Temperature, dan memiliki harga di 0.70-0.75
        self.assertEqual(len(results), 2)

    def test_empty_query_returns_all(self):
        """Query kosong mengembalikan semua market (kecuali difilter)."""
        results = search_markets(self.mock_markets, query="", now=self.now)
        self.assertEqual(len(results), len(self.mock_markets))

    def test_no_match_returns_empty(self):
        """Pencarian keyword yang tidak cocok mengembalikan list kosong."""
        results = search_markets(self.mock_markets, query="Jakarta", now=self.now)
        self.assertEqual(len(results), 0)

    def test_response_fields(self):
        """Memastikan response memuat seluruh field yang dispesifikasikan."""
        results = search_markets(self.mock_markets, query="snow", now=self.now)
        self.assertEqual(len(results), 1)
        item = results[0]

        expected_fields = {
            "market_id", "market_name", "category", "current_price",
            "price_yes", "price_no", "resolution_time", "time_remaining"
        }
        for field in expected_fields:
            self.assertIn(field, item, f"Missing field: {field}")

        self.assertEqual(item["category"], "Snow")
        self.assertEqual(item["current_price"], 0.75)
        self.assertEqual(item["price_yes"], 0.75)
        self.assertEqual(item["price_no"], 0.25)

    def test_orm_snapshot_support(self):
        """Kompatibilitas dengan objek model ORM MarketSnapshot."""
        orm_market = MarketSnapshot(
            market_id="mkt-orm-search",
            market_name="Will Seattle rain > 0.1 in on Sep 6?",
            category="Precipitation",
            status="open",
            price_yes=Decimal("0.60"),
            price_no=Decimal("0.40"),
            current_price=Decimal("0.60"),
        )
        results = search_markets([orm_market], query="seattle", now=self.now)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["market_id"], "mkt-orm-search")
        self.assertEqual(results[0]["category"], "Precipitation")

    def test_fastapi_search_endpoint_execution(self):
        """Uji eksekusi handler FastAPI GET /api/markets/search."""
        with patch("app.paper_service.get_market_snapshots", return_value=self.mock_markets):
            res = search_markets_api(q="wind")
            self.assertIsInstance(res, list)
            self.assertTrue(len(res) > 0)
            for item in res:
                self.assertIn("market_id", item)
                self.assertIn("market_name", item)
                self.assertIn("category", item)
                self.assertTrue(
                    "wind" in item["market_name"].lower() or "wind" in item["category"].lower()
                )

    def test_no_settlement_engine_imported(self):
        """Verifikasi mutlak: modul suggestions/search TIDAK mengimpor settlement_engine."""
        import app.paper_trading.suggestions as mod
        self.assertNotIn("settlement_engine", dir(mod))
        self.assertNotIn("calculate_settlement", dir(mod))


if __name__ == "__main__":
    unittest.main()
