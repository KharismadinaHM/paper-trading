"""
Unit tests for Polymarket Positions, Sell Execution, Dynamic PnL, Deposit, and Search Filters.
"""
from decimal import Decimal
from unittest.mock import patch
import pytest

from app.dashboard import (
    DepositFundsRequest,
    SellPositionRequest,
    deposit_funds_api,
    reset_account_api,
    search_markets_api,
    sell_position_api,
)
from app.paper_service import (
    deposit_paper_funds,
    get_account_status,
    get_open_positions,
    get_performance,
    get_trade_history,
    reset_paper_account,
    sell_paper_position,
    search_market_snapshots,
)


class TestPolymarketPositionsAndTrading:

    def setup_method(self):
        reset_paper_account()

    def test_default_positions_dynamic_valuation(self):
        """Memastikan posisi default memiliki valuasi dynamic Mark-to-Market."""
        positions = get_open_positions()
        assert len(positions) >= 3
        for p in positions:
            assert "market" in p
            assert "side" in p
            assert "entry_price" in p
            assert "shares" in p
            assert "current_price" in p
            assert "current_value" in p
            assert "to_win" in p
            assert "unrealized_pnl" in p
            assert "roi_pct" in p
            assert "avg_to_now" in p
            assert "¢" in p["avg_to_now"]
            assert "polymarket_url" in p
            assert p["polymarket_url"].startswith("https://polymarket.com/markets?_q=")

    def test_sell_paper_position_full(self):
        """Memastikan sell position menjual seluruh shares dan mengkredit cash balance."""
        positions = get_open_positions()
        target = positions[0]
        initial_balance = get_account_status()["balance"]

        res = sell_paper_position(
            market_id=target["market_id"],
            side=target["side"],
        )
        assert res["success"] is True
        assert res["shares_sold"] == float(target["shares"])
        assert res["proceeds"] > 0
        new_balance = get_account_status()["balance"]
        assert new_balance == initial_balance + Decimal(str(res["proceeds"]))

        # Posisi harus berkurang
        remaining_ids = [p["market_id"] for p in get_open_positions()]
        assert target["market_id"] not in remaining_ids

        # Harus tercatat di trade history
        trades = get_trade_history()
        assert trades[0]["market_id"] == target["market_id"]
        assert trades[0]["status"] in ["WON", "LOST"]

    def test_sell_paper_position_partial(self):
        """Memastikan partial sell mengurangi shares dan cost basis proporsional."""
        positions = get_open_positions()
        target = positions[0]
        target_shares = Decimal(str(target["shares"]))
        half_shares = (target_shares / Decimal("2")).quantize(Decimal("0.01"))

        res = sell_paper_position(
            market_id=target["market_id"],
            side=target["side"],
            shares_to_sell=half_shares,
        )
        assert res["success"] is True
        assert res["shares_sold"] == float(half_shares)

        # Posisi masih ada dengan sisa shares
        updated_positions = {p["market_id"]: p for p in get_open_positions()}
        assert target["market_id"] in updated_positions
        rem_shares = Decimal(str(updated_positions[target["market_id"]]["shares"]))
        assert abs(rem_shares - (target_shares - half_shares)) < Decimal("0.02")

    def test_sell_invalid_market_raises_error(self):
        """Menjual market yang tidak ada melempar ValueError."""
        with pytest.raises(ValueError) as exc:
            sell_paper_position(market_id="invalid_xyz", side="YES")
        assert "tidak ditemukan" in str(exc.value).lower()

    def test_deposit_paper_funds(self):
        """Menambah saldo akun paper trading."""
        initial_bal = get_account_status()["balance"]
        deposit_paper_funds(Decimal("50.00"))
        new_bal = get_account_status()["balance"]
        assert new_bal == initial_bal + Decimal("50.00")

    def test_api_sell_endpoint(self):
        """Menguji endpoint POST /api/positions/sell via fungsi handler."""
        positions = get_open_positions()
        target = positions[0]
        req = SellPositionRequest(
            market_id=target["market_id"],
            side=target["side"],
        )
        res = sell_position_api(req)
        assert res["success"] is True
        assert res["proceeds"] > 0

    def test_api_deposit_endpoint(self):
        """Menguji endpoint POST /api/account/deposit via fungsi handler."""
        req = DepositFundsRequest(amount=25.0)
        res = deposit_funds_api(req)
        assert res["success"] is True
        assert res["amount"] == 25.0

    def test_api_reset_endpoint(self):
        """Menguji endpoint POST /api/account/reset via fungsi handler."""
        res = reset_account_api()
        assert res["success"] is True
        assert get_account_status()["balance"] == Decimal("20.00")

    def test_api_search_markets_with_time_filter(self):
        """Menguji filter waktu dan sorting di search_markets_api."""
        res = search_markets_api(time_filter="24h", sort_by="ending_soonest")
        assert isinstance(res, list)
