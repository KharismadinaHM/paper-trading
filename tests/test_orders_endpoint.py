import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from fastapi import HTTPException

from app.core.config import settings
from app.dashboard import CreateOrderRequest, create_order_api, app
from app.paper_service import (
    create_paper_order,
    get_account_status,
    get_market_by_id,
    get_paper_orders,
    reset_paper_account,
    _account_state,
    _paper_orders,
)


class TestOrdersEndpoint(unittest.TestCase):

    def setUp(self):
        reset_paper_account()
        self.now = datetime(2026, 9, 4, 10, 0, 0, tzinfo=timezone.utc)
        self.mock_stub_markets = {
            "mkt-nyc-85f-0905": {
                "market_id": "mkt-nyc-85f-0905",
                "market_name": "Will NYC exceed 85°F on Sep 5?",
                "category": "Temperature",
                "status": "open",
                "is_resolved": False,
                "resolution_time": self.now + timedelta(hours=3),
                "price_yes": Decimal("0.72"),
                "price_no": Decimal("0.28"),
                "current_price": Decimal("0.72"),
                "timestamp": self.now,
                "is_stale": False,
            },
            "mkt-seattle-rain-0904": {
                "market_id": "mkt-seattle-rain-0904",
                "market_name": "Will Seattle rain > 0.1 in on Sep 4?",
                "category": "Precipitation",
                "status": "resolved",
                "is_resolved": True,
                "resolution_time": self.now - timedelta(hours=1),
                "price_yes": Decimal("0.72"),
                "price_no": Decimal("0.28"),
                "current_price": Decimal("1.00"),
                "timestamp": self.now,
                "is_stale": False,
            },
        }

    def tearDown(self):
        reset_paper_account()

    def test_fastapi_route_registered(self):
        """Memastikan route POST /api/orders terdaftar di FastAPI app."""
        routes = [route.path for route in app.routes]
        self.assertIn("/api/orders", routes)

    def test_anti_stale_price_user_viewed_vs_realtime_execution(self):
        """
        SKENARIO UTAMA ANTI-STALE PRICE:
        User melihat harga $0.74 di dashboard saat klik buy,
        tetapi saat submit harga market real-time sudah bergerak menjadi $0.78.
        
        Verifikasi:
        1. entry_price yang disimpan BUKAN $0.74, melainkan $0.78 (atau setelah slippage dari $0.78).
        2. shares dihitung dari $0.78, BUKAN $0.74.
        3. Selisih harga (>5%) menghasilkan warning transparan ke user.
        4. entry_price dari payload frontend hanya dicatat di requested_price untuk audit trail.
        """
        mock_real_time_market = {
            "market_id": "mkt-test-stale",
            "market_name": "Will Dallas hit 100°F on Sep 5?",
            "category": "Temperature",
            "status": "open",
            "is_resolved": False,
            "resolution_time": self.now + timedelta(hours=3),
            "price_yes": Decimal("0.78"),
            "price_no": Decimal("0.22"),
            "current_price": Decimal("0.78"),
        }

        # Override SLIPPAGE_BPS=0 dan SPREAD_BPS=0 untuk trace numerik presisi
        with patch.object(settings, "SLIPPAGE_BPS", 0), \
             patch.object(settings, "SPREAD_BPS", 0), \
             patch("app.paper_service.get_market_by_id", return_value=mock_real_time_market):

            request_payload = CreateOrderRequest(
                market_id="mkt-test-stale",
                side="YES",
                position_size=1.00,
                entry_price=0.74,  # User melihat $0.74 (stale price)
            )

            response = create_order_api(request_payload)

            # 1. Pastikan entry_price aktual yang dipakai adalah $0.78, BUKAN $0.74!
            self.assertEqual(response.actual_price, 0.78)
            self.assertEqual(response.entry_price, 0.78)
            self.assertNotEqual(response.entry_price, 0.74)

            # 2. Pastikan requested_price mencatat $0.74 hanya untuk audit trail
            self.assertEqual(response.requested_price, 0.74)

            # 3. Pastikan kalkulasi shares: 1.00 / 0.78 = 1.2821 (BUKAN 1.00 / 0.74 = 1.3514)
            expected_shares_078 = (Decimal("1.00") / Decimal("0.78")).quantize(Decimal("0.0001"))
            self.assertEqual(Decimal(str(response.shares)), expected_shares_078)
            self.assertEqual(response.shares, 1.2821)
            self.assertNotEqual(response.shares, 1.3514)

            # 4. Pastikan status order adalah OPEN
            self.assertEqual(response.status, "OPEN")

            # 5. Pastikan warning muncul karena selisih (0.78 - 0.74) / 0.74 = 5.41% > 5%
            self.assertIsNotNone(response.warning)
            self.assertIn("Warning: Terjadi pergerakan harga pasar", response.warning)
            self.assertIn("5.4%", response.warning)

            # 6. Pastikan data di database / repository menyimpan entry_price $0.78
            orders = get_paper_orders()
            self.assertEqual(len(orders), 1)
            saved_order = orders[0]
            self.assertEqual(saved_order["entry_price"], Decimal("0.78"))
            self.assertNotEqual(saved_order["entry_price"], Decimal("0.74"))

    def test_anti_stale_price_no_warning_when_movement_below_threshold(self):
        """
        Jika pergerakan harga kecil (misal user lihat $0.75, real-time $0.76 -> 1.3% <= 5%),
        order berhasil tanpa warning.
        """
        mock_market = {
            "market_id": "mkt-test-small-move",
            "market_name": "Will Austin rain?",
            "status": "open",
            "is_resolved": False,
            "price_yes": Decimal("0.76"),
            "price_no": Decimal("0.24"),
            "current_price": Decimal("0.76"),
        }

        with patch.object(settings, "SLIPPAGE_BPS", 0), \
             patch.object(settings, "SPREAD_BPS", 0), \
             patch("app.paper_service.get_market_by_id", return_value=mock_market):

            request_payload = CreateOrderRequest(
                market_id="mkt-test-small-move",
                side="YES",
                position_size=1.00,
                entry_price=0.75,  # Selisih hanya 1.33%
            )

            response = create_order_api(request_payload)
            self.assertEqual(response.entry_price, 0.76)
            self.assertIsNone(response.warning)

    def test_market_not_found_returns_404(self):
        """Order untuk market_id yang tidak ada harus ditolak dengan HTTP 404."""
        request_payload = CreateOrderRequest(
            market_id="mkt-non-existent-9999",
            side="YES",
            position_size=1.00,
            entry_price=0.70,
        )

        with self.assertRaises(HTTPException) as ctx:
            create_order_api(request_payload)

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("tidak ditemukan", ctx.exception.detail)

    def test_resolved_market_returns_400(self):
        """Order untuk market yang sudah resolved harus ditolak dengan HTTP 400."""
        # mkt-seattle-rain-0904 di stub data berstatus resolved
        request_payload = CreateOrderRequest(
            market_id="mkt-seattle-rain-0904",
            side="YES",
            position_size=1.00,
            entry_price=0.72,
        )

        with patch("app.paper_service.get_market_by_id", side_effect=lambda mid, **kw: self.mock_stub_markets.get(mid)), \
             self.assertRaises(HTTPException) as ctx:
            create_order_api(request_payload)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("sudah resolved", ctx.exception.detail)

    def test_risk_rejection_insufficient_balance_no_partial_write(self):
        """
        ATURAN RISK CONTROL & ATOMICITY:
        Jika position_size melebihi available balance, evaluate_risk_and_rules menolak order.
        Verifikasi: Langsung raise HTTP 400, dan TIDAK ADA order yang tersimpan di DB!
        """
        # Saldo diset $0.50, request position_size $1.00 (lolos max_position_size $1.00 tapi saldo tidak cukup)
        _account_state["balance"] = Decimal("0.50")
        request_payload = CreateOrderRequest(
            market_id="mkt-nyc-85f-0905",
            side="YES",
            position_size=1.00,
            entry_price=0.72,
        )

        initial_orders_count = len(get_paper_orders())
        initial_balance = get_account_status()["balance"]

        with patch("app.paper_service.get_market_by_id", side_effect=lambda mid, **kw: self.mock_stub_markets.get(mid)), \
             self.assertRaises(HTTPException) as ctx:
            create_order_api(request_payload)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Insufficient balance", ctx.exception.detail)

        # CONFIRM: TIDAK ADA PARTIAL WRITE
        self.assertEqual(len(get_paper_orders()), initial_orders_count)
        self.assertEqual(get_account_status()["balance"], initial_balance)

    def test_risk_rejection_exceeds_max_position_size_no_partial_write(self):
        """
        Jika position_size melebihi MAX_POSITION_SIZE ($1.00), order ditolak tanpa partial write.
        """
        with patch.object(settings, "MAX_POSITION_SIZE", Decimal("1.00")), \
             patch("app.paper_service.get_market_by_id", side_effect=lambda mid, **kw: self.mock_stub_markets.get(mid)):
            request_payload = CreateOrderRequest(
                market_id="mkt-nyc-85f-0905",
                side="YES",
                position_size=5.00,  # Melebihi max $1.00
                entry_price=0.72,
            )

            initial_orders_count = len(get_paper_orders())

            with self.assertRaises(HTTPException) as ctx:
                create_order_api(request_payload)

            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("exceeds max", ctx.exception.detail)
            # CONFIRM: Tidak ada order tersimpan
            self.assertEqual(len(get_paper_orders()), initial_orders_count)

    def test_execution_sequence_and_risk_call_before_shares_and_db(self):
        """
        VERIFIKASI URUTAN EKSEKUSI WAJIB:
        1. get_market_by_id (fetch real-time)
        2. apply_slippage_and_spread
        3. evaluate_risk_and_rules
        4. calculate_shares
        5. simpan DB
        
        Jika evaluate_risk_and_rules gagal, calculate_shares TIDAK PERNAH dipanggil!
        """
        with patch("app.paper_service.get_market_by_id", side_effect=lambda mid, **kw: self.mock_stub_markets.get(mid)), \
             patch("app.paper_service.evaluate_risk_and_rules", return_value=(False, "REJECTED: Risk trigger test")) as mock_risk, \
             patch("app.paper_service.calculate_shares") as mock_shares:

            with self.assertRaises(ValueError) as ctx:
                create_paper_order(
                    market_id="mkt-nyc-85f-0905",
                    side="YES",
                    position_size=Decimal("1.00"),
                    user_viewed_price=Decimal("0.72"),
                )

            self.assertIn("Risk trigger test", str(ctx.exception))
            # evaluate_risk_and_rules dipanggil
            mock_risk.assert_called_once()
            # calculate_shares TIDAK PERNAH dipanggil karena risk reject
            mock_shares.assert_not_called()
            # Database TIDAK menyimpan order
            self.assertEqual(len(get_paper_orders()), 0)

    def test_side_no_uses_price_no_realtime(self):
        """Order side NO harus mengambil price_no real-time."""
        mock_market = {
            "market_id": "mkt-austin-test",
            "market_name": "Will Austin high > 95°F on Sep 5?",
            "status": "open",
            "is_resolved": False,
            "price_yes": Decimal("0.26"),
            "price_no": Decimal("0.74"),
            "current_price": Decimal("0.74"),
        }

        with patch.object(settings, "SLIPPAGE_BPS", 0), \
             patch.object(settings, "SPREAD_BPS", 0), \
             patch("app.paper_service.get_market_by_id", return_value=mock_market):

            res = create_paper_order(
                market_id="mkt-austin-test",
                side="NO",
                position_size=Decimal("1.00"),
                user_viewed_price=Decimal("0.74"),
            )

            self.assertEqual(res["actual_price"], Decimal("0.74"))
            self.assertEqual(res["side"], "NO")
            self.assertEqual(res["status"], "OPEN")


if __name__ == "__main__":
    unittest.main()
