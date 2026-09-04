import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from app.paper_trading.models import MarketSnapshot
from app.paper_trading.suggestions import (
    filter_market_suggestions,
    format_time_remaining,
)
from app.dashboard import get_market_suggestions_api
from app.paper_service import get_market_suggestions


class TestMarketSuggestionsFilter(unittest.TestCase):

    def setUp(self):
        # Base reference time (fixed for deterministic test execution)
        self.now = datetime(2026, 9, 4, 10, 0, 0, tzinfo=timezone.utc)

        # Mock dataset with various permutations of market conditions
        self.mock_markets = [
            # 1. Valid: Open, 3h remaining, YES price = 0.73 -> QUALIFIES (YES)
            {
                "market_id": "mkt-valid-yes",
                "market_name": "Will NYC exceed 85°F on Sep 5?",
                "status": "open",
                "is_resolved": False,
                "resolution_time": self.now + timedelta(hours=3),
                "price_yes": Decimal("0.73"),
                "price_no": Decimal("0.27"),
                "current_price": Decimal("0.73"),
            },
            # 2. Valid: Open, 2h remaining, NO price = 0.72 -> QUALIFIES (NO)
            {
                "market_id": "mkt-valid-no",
                "market_name": "Will Austin high > 95°F on Sep 5?",
                "status": "open",
                "is_resolved": False,
                "resolution_time": self.now + timedelta(hours=2),
                "price_yes": Decimal("0.28"),
                "price_no": Decimal("0.72"),
                "current_price": Decimal("0.72"),
            },
            # 3. Invalid: Already resolved flag True -> EXCLUDED
            {
                "market_id": "mkt-resolved-bool",
                "market_name": "Will Chicago wind > 25mph?",
                "status": "open",
                "is_resolved": True,
                "resolution_time": self.now + timedelta(hours=2),
                "price_yes": Decimal("0.72"),
                "price_no": Decimal("0.28"),
            },
            # 4. Invalid: Status is 'resolved' -> EXCLUDED
            {
                "market_id": "mkt-status-resolved",
                "market_name": "Will Miami rain > 0.5 in?",
                "status": "resolved",
                "is_resolved": False,
                "resolution_time": self.now + timedelta(hours=2),
                "price_yes": Decimal("0.74"),
                "price_no": Decimal("0.26"),
            },
            # 5. Invalid: Status is 'closed' -> EXCLUDED
            {
                "market_id": "mkt-status-closed",
                "market_name": "Will Dallas temp > 90°F?",
                "status": "closed",
                "is_resolved": False,
                "resolution_time": self.now + timedelta(hours=1),
                "price_yes": Decimal("0.71"),
                "price_no": Decimal("0.29"),
            },
            # 6. Invalid: Resolution time > max_hours (10h > 6h) -> EXCLUDED with default
            {
                "market_id": "mkt-too-far",
                "market_name": "Will Boston temp drop < 50°F?",
                "status": "open",
                "is_resolved": False,
                "resolution_time": self.now + timedelta(hours=10),
                "price_yes": Decimal("0.74"),
                "price_no": Decimal("0.26"),
            },
            # 7. Invalid: Resolution time in the past (-1h) -> EXCLUDED
            {
                "market_id": "mkt-in-past",
                "market_name": "Will Seattle rain > 0.1 in yesterday?",
                "status": "open",
                "is_resolved": False,
                "resolution_time": self.now - timedelta(hours=1),
                "price_yes": Decimal("0.72"),
                "price_no": Decimal("0.28"),
            },
            # 8. Invalid: Price below range (0.65 < 0.70) -> EXCLUDED
            {
                "market_id": "mkt-price-low",
                "market_name": "Will Phoenix exceed 105°F?",
                "status": "open",
                "is_resolved": False,
                "resolution_time": self.now + timedelta(hours=4),
                "price_yes": Decimal("0.65"),
                "price_no": Decimal("0.35"),
            },
            # 9. Invalid: Price above range (0.80 > 0.75) -> EXCLUDED
            {
                "market_id": "mkt-price-high",
                "market_name": "Will Las Vegas reach 100°F?",
                "status": "open",
                "is_resolved": False,
                "resolution_time": self.now + timedelta(hours=4),
                "price_yes": Decimal("0.80"),
                "price_no": Decimal("0.20"),
            },
            # 10. Valid: Boundary lower limit (price = 0.70) -> QUALIFIES
            {
                "market_id": "mkt-boundary-lower",
                "market_name": "Will Denver snow > 1 in?",
                "status": "open",
                "is_resolved": False,
                "resolution_time": self.now + timedelta(hours=5),
                "price_yes": Decimal("0.70"),
                "price_no": Decimal("0.30"),
            },
            # 11. Valid: Boundary upper limit (price = 0.75) -> QUALIFIES
            {
                "market_id": "mkt-boundary-upper",
                "market_name": "Will Atlanta rain > 0.2 in?",
                "status": "open",
                "is_resolved": False,
                "resolution_time": self.now + timedelta(hours=1),
                "price_yes": Decimal("0.25"),
                "price_no": Decimal("0.75"),
            },
            # 12. Invalid: Just outside boundary lower (price = 0.699) -> EXCLUDED
            {
                "market_id": "mkt-outside-lower",
                "market_name": "Will Portland rain > 0.5 in?",
                "status": "open",
                "is_resolved": False,
                "resolution_time": self.now + timedelta(hours=3),
                "price_yes": Decimal("0.699"),
                "price_no": Decimal("0.301"),
            },
            # 13. Invalid: Just outside boundary upper (price = 0.751) -> EXCLUDED
            {
                "market_id": "mkt-outside-upper",
                "market_name": "Will Houston humidity > 80%?",
                "status": "open",
                "is_resolved": False,
                "resolution_time": self.now + timedelta(hours=3),
                "price_yes": Decimal("0.751"),
                "price_no": Decimal("0.249"),
            },
        ]

    def test_filter_suggestions_default_criteria(self):
        """Uji filter standar: max 6 jam, harga 0.70 - 0.75."""
        results = filter_market_suggestions(
            self.mock_markets,
            max_hours_to_resolution=6.0,
            min_price=0.70,
            max_price=0.75,
            now=self.now,
        )

        # Hanya 4 market yang harus lolos:
        # mkt-valid-yes (0.73, 3h), mkt-valid-no (0.72, 2h), mkt-boundary-lower (0.70, 5h), mkt-boundary-upper (0.75, 1h)
        passing_ids = [m["market_id"] for m in results]
        self.assertEqual(len(results), 4)
        self.assertIn("mkt-valid-yes", passing_ids)
        self.assertIn("mkt-valid-no", passing_ids)
        self.assertIn("mkt-boundary-lower", passing_ids)
        self.assertIn("mkt-boundary-upper", passing_ids)

        # Pastikan market yang tidak sesuai kriteria tidak lolos
        self.assertNotIn("mkt-resolved-bool", passing_ids)
        self.assertNotIn("mkt-status-resolved", passing_ids)
        self.assertNotIn("mkt-status-closed", passing_ids)
        self.assertNotIn("mkt-too-far", passing_ids)
        self.assertNotIn("mkt-in-past", passing_ids)
        self.assertNotIn("mkt-price-low", passing_ids)
        self.assertNotIn("mkt-price-high", passing_ids)
        self.assertNotIn("mkt-outside-lower", passing_ids)
        self.assertNotIn("mkt-outside-upper", passing_ids)

    def test_response_fields(self):
        """Pastikan seluruh field yang diminta tersedia dalam response."""
        results = filter_market_suggestions(self.mock_markets, now=self.now)
        self.assertTrue(len(results) > 0)

        required_keys = {"market_id", "market_name", "current_price", "resolution_time", "time_remaining"}
        for item in results:
            self.assertTrue(required_keys.issubset(item.keys()), f"Missing keys in {item}")
            self.assertIsInstance(item["market_id"], str)
            self.assertIsInstance(item["market_name"], str)
            self.assertIsInstance(item["current_price"], (float, Decimal))
            self.assertIsInstance(item["resolution_time"], str)
            self.assertIsInstance(item["time_remaining"], str)
            self.assertIn("h ", item["time_remaining"])

    def test_custom_max_hours_parameter(self):
        """Uji fleksibilitas parameter max_hours_to_resolution."""
        # Dengan max_hours=12 jam, mkt-too-far (10h) harus ikut lolos
        results_12h = filter_market_suggestions(
            self.mock_markets,
            max_hours_to_resolution=12.0,
            now=self.now,
        )
        passing_ids_12h = [m["market_id"] for m in results_12h]
        self.assertIn("mkt-too-far", passing_ids_12h)

        # Dengan max_hours=2.5 jam, mkt-valid-yes (3h), mkt-boundary-lower (5h), mkt-too-far (10h) tidak boleh lolos
        results_2h = filter_market_suggestions(
            self.mock_markets,
            max_hours_to_resolution=2.5,
            now=self.now,
        )
        passing_ids_2h = [m["market_id"] for m in results_2h]
        self.assertIn("mkt-valid-no", passing_ids_2h)  # 2h
        self.assertIn("mkt-boundary-upper", passing_ids_2h)  # 1h
        self.assertNotIn("mkt-valid-yes", passing_ids_2h)  # 3h
        self.assertNotIn("mkt-boundary-lower", passing_ids_2h)  # 5h

    def test_custom_price_range_parameter(self):
        """Uji fleksibilitas parameter min_price dan max_price custom."""
        # Cari harga di rentang 0.60 - 0.68
        results_custom = filter_market_suggestions(
            self.mock_markets,
            min_price=0.60,
            max_price=0.68,
            now=self.now,
        )
        passing_ids = [m["market_id"] for m in results_custom]
        self.assertIn("mkt-price-low", passing_ids)  # 0.65
        self.assertNotIn("mkt-valid-yes", passing_ids)  # 0.73

    def test_orm_model_support(self):
        """Uji kompatibilitas dengan model SQLAlchemy MarketSnapshot."""
        orm_market = MarketSnapshot(
            market_id="mkt-orm-001",
            market_name="Will Orlando thunderstorm on Sep 5?",
            status="open",
            is_resolved=False,
            resolution_time=self.now + timedelta(hours=4),
            price_yes=Decimal("0.74"),
            price_no=Decimal("0.26"),
            current_price=Decimal("0.74"),
        )
        results = filter_market_suggestions([orm_market], now=self.now)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["market_id"], "mkt-orm-001")
        self.assertEqual(results[0]["current_price"], 0.74)
        self.assertEqual(results[0]["side"], "YES")

    def test_iso_string_resolution_time(self):
        """Uji kompatibilitas jika resolution_time berupa string ISO 8601."""
        str_market = {
            "market_id": "mkt-iso-001",
            "market_name": "Will Tampa temp > 88°F?",
            "status": "open",
            "is_resolved": False,
            "resolution_time": (self.now + timedelta(hours=3, minutes=15)).isoformat(),
            "price_yes": "0.71",
        }
        results = filter_market_suggestions([str_market], now=self.now)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["market_id"], "mkt-iso-001")
        self.assertEqual(results[0]["time_remaining"], "3h 15m")

    def test_missing_or_invalid_resolution_time(self):
        """Uji penanganan defensif jika resolution_time kosong atau rusak."""
        invalid_market = {
            "market_id": "mkt-no-time",
            "market_name": "Market without time",
            "status": "open",
            "is_resolved": False,
            "resolution_time": None,
            "price_yes": "0.72",
        }
        corrupt_market = {
            "market_id": "mkt-bad-time",
            "market_name": "Market with corrupt time",
            "status": "open",
            "is_resolved": False,
            "resolution_time": "bukan-tanggal-valid",
            "price_yes": "0.72",
        }
        results = filter_market_suggestions([invalid_market, corrupt_market], now=self.now)
        self.assertEqual(len(results), 0)

    def test_invalid_price_handled_safely(self):
        """Uji penanganan defensif jika format harga bukan angka yang valid."""
        bad_price_market = {
            "market_id": "mkt-bad-price",
            "market_name": "Market with non-numeric price",
            "status": "open",
            "is_resolved": False,
            "resolution_time": self.now + timedelta(hours=2),
            "price_yes": "invalid_number",
        }
        results = filter_market_suggestions([bad_price_market], now=self.now)
        self.assertEqual(len(results), 0)

    def test_timezone_naive_resolution_time(self):
        """Uji penanganan resolution_time naive datetime agar tidak crash dengan aware now."""
        naive_market = {
            "market_id": "mkt-naive-dt",
            "market_name": "Market with naive datetime",
            "status": "open",
            "is_resolved": False,
            "resolution_time": datetime(2026, 9, 4, 13, 0, 0),  # Naive (3 jam setelah self.now)
            "price_yes": Decimal("0.72"),
        }
        results = filter_market_suggestions([naive_market], now=self.now)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["market_id"], "mkt-naive-dt")
        self.assertEqual(results[0]["time_remaining"], "3h 00m")

    def test_format_time_remaining_helper(self):
        """Uji format helper waktu tersisa."""
        self.assertEqual(format_time_remaining(3600), "1h 00m")
        self.assertEqual(format_time_remaining(3665), "1h 01m")
        self.assertEqual(format_time_remaining(180), "0h 03m")
        self.assertEqual(format_time_remaining(0), "0h 00m")
        self.assertEqual(format_time_remaining(-50), "0h 00m")

    def test_fastapi_endpoint_execution(self):
        """Uji pemanggilan handler endpoint FastAPI GET /api/markets/suggestions."""
        with patch("app.paper_service.get_market_snapshots", return_value=self.mock_markets):
            endpoint_res = get_market_suggestions_api(
                max_hours_to_resolution=6.0,
                min_price=0.70,
                max_price=0.75,
            )
            self.assertIsInstance(endpoint_res, list)
            self.assertTrue(len(endpoint_res) > 0)
            first = endpoint_res[0]
            self.assertIn("market_id", first)
            self.assertIn("market_name", first)
            self.assertIn("current_price", first)
            self.assertIn("resolution_time", first)
            self.assertIn("time_remaining", first)

    def test_fastapi_endpoint_tight_max_hours(self):
        """Uji endpoint FastAPI jika diberikan max_hours sangat kecil."""
        # 1 jam -> kemungkinan tidak ada yang lolos karena minimum stub data 2 jam
        with patch("app.paper_service.get_market_snapshots", return_value=self.mock_markets):
            res_tight = get_market_suggestions_api(max_hours_to_resolution=1.0)
            self.assertEqual(len(res_tight), 0)

    def test_no_settlement_engine_imported(self):
        """
        Verifikasi batasan ketat: modul suggestions dan endpoint TIDAK
        mengimpor atau memanggil settlement_engine.py.
        """
        import app.paper_trading.suggestions as suggestions_mod
        self.assertNotIn("settlement_engine", dir(suggestions_mod))
        self.assertNotIn("calculate_settlement", dir(suggestions_mod))
        self.assertNotIn("calculate_shares", dir(suggestions_mod))

        # Periksa juga bahwa settlement_engine tidak tercemar di namespace dashboard
        import app.dashboard as dashboard_mod
        self.assertNotIn("settlement_engine", dir(dashboard_mod))
        self.assertNotIn("calculate_settlement", dir(dashboard_mod))

    def test_dashboard_page_renders_suggested_markets(self):
        """Uji rendering template dashboard agar Suggested Markets dan modal Paper Buy tampil."""
        from starlette.requests import Request
        from app.dashboard import get_dashboard

        req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
        response = get_dashboard(req)

        self.assertEqual(response.status_code, 200)
        html_body = response.body.decode("utf-8")
        self.assertIn("Suggested Markets", html_body)
        self.assertIn("Paper Buy", html_body)
        self.assertIn("paperBuyModal", html_body)
        self.assertIn("position_size", html_body)
        self.assertIn("/api/markets/suggestions", html_body)


if __name__ == "__main__":
    unittest.main()

