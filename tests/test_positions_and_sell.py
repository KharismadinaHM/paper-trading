"""
Unit tests for Polymarket Positions, Sell Execution, Dynamic PnL, Deposit, and Search Filters.
Semua state dibaca/ditulis ke database test (lihat tests/conftest.py).
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.core.database import get_db_session
from app.dashboard import (
    DepositFundsRequest,
    SellPositionRequest,
    deposit_funds_api,
    reset_account_api,
    search_markets_api,
    sell_position_api,
)
from app.paper_service import (
    create_paper_order,
    deposit_paper_funds,
    get_account_status,
    get_open_positions,
    get_trade_history,
    reset_paper_account,
    sell_paper_position,
)
from app.paper_trading.models import MarketSnapshot


def add_snapshot(market_id, price_yes, name=None, minutes_ago=0, hours_to_resolution=5, **extra):
    now = datetime.now(timezone.utc)
    db = get_db_session()
    try:
        db.add(MarketSnapshot(
            id=uuid.uuid4(),
            market_id=market_id,
            market_name=name or f"Market {market_id}",
            status=extra.pop("status", "open"),
            is_resolved=extra.pop("is_resolved", False),
            resolution_time=now + timedelta(hours=hours_to_resolution),
            price_yes=Decimal(str(price_yes)),
            price_no=Decimal("1") - Decimal(str(price_yes)),
            current_price=Decimal(str(price_yes)),
            timestamp=now - timedelta(minutes=minutes_ago),
            **extra,
        ))
        db.commit()
    finally:
        db.close()


class TestPolymarketPositionsAndTrading:

    def setup_method(self):
        reset_paper_account()
        add_snapshot("0xnyc", "0.60", name="Will NYC exceed 85°F?")
        add_snapshot("0xhk", "0.30", name="Will Hong Kong be 34°C or above?")
        create_paper_order("0xnyc", "YES", Decimal("1.00"))
        create_paper_order("0xhk", "NO", Decimal("1.00"))

    def test_positions_dynamic_valuation(self):
        """Posisi memiliki valuasi Mark-to-Market dinamis dari snapshot terbaru."""
        add_snapshot("0xnyc", "0.75", name="Will NYC exceed 85°F?")
        positions = {p["market_id"]: p for p in get_open_positions()}
        assert set(positions) == {"0xnyc", "0xhk"}
        nyc = positions["0xnyc"]
        assert nyc["side"] == "YES"
        assert nyc["current_price"] == Decimal("0.75")
        # 1.00 / 0.60 = 1.6667 shares x 0.75 = 1.2500
        assert nyc["current_value"] == Decimal("1.2500")
        assert nyc["unrealized_pnl"] == Decimal("0.2500")
        assert "¢" in nyc["avg_to_now"]
        assert nyc["polymarket_url"].startswith("https://polymarket.com/markets?_q=")
        assert positions["0xhk"]["current_price"] == Decimal("0.70")

    def test_sell_paper_position_full(self):
        """Sell seluruh shares mengkredit cash balance dan mencatat trade CLOSED."""
        initial_balance = get_account_status()["balance"]
        res = sell_paper_position(market_id="0xnyc", side="YES")
        assert res["success"] is True
        assert res["proceeds"] == pytest.approx(1.0, abs=0.0001)
        assert get_account_status()["balance"] == initial_balance + Decimal(str(res["proceeds"]))
        assert "0xnyc" not in [p["market_id"] for p in get_open_positions()]

        trades = get_trade_history()
        assert trades[0]["market_id"] == "0xnyc"
        assert trades[0]["status"] == "CLOSED"

    def test_sell_paper_position_partial(self):
        """Partial sell mengurangi shares dan cost basis secara proporsional."""
        target = next(p for p in get_open_positions() if p["market_id"] == "0xnyc")
        half = (target["shares"] / 2).quantize(Decimal("0.0001"))
        res = sell_paper_position(market_id="0xnyc", side="YES", shares_to_sell=half)
        assert res["shares_sold"] == float(half)
        assert res["cost_basis"] == pytest.approx(0.5, abs=0.0001)

        remaining = next(p for p in get_open_positions() if p["market_id"] == "0xnyc")
        assert remaining["shares"] == target["shares"] - half
        assert remaining["size"] == Decimal("0.5000")

    def test_sell_invalid_market_raises_error(self):
        with pytest.raises(ValueError) as exc:
            sell_paper_position(market_id="invalid_xyz", side="YES")
        assert "tidak ditemukan" in str(exc.value).lower()

    def test_sell_wrong_side_does_not_sell_other_side(self):
        """Regresi: sell YES pada posisi yang hanya NO harus ditolak, bukan menjual posisi NO."""
        with pytest.raises(ValueError) as exc:
            sell_paper_position(market_id="0xhk", side="YES")
        assert "tidak ditemukan" in str(exc.value).lower()
        assert "0xhk" in [p["market_id"] for p in get_open_positions()]

    def test_sell_more_than_owned_rejected(self):
        with pytest.raises(ValueError):
            sell_paper_position(market_id="0xnyc", side="YES", shares_to_sell=Decimal("100"))

    def test_sell_below_minimum_notional_rejected(self):
        """Regresi: sell 0.0001 share tidak boleh tercatat sebagai trade $0 WON."""
        with pytest.raises(ValueError) as exc:
            sell_paper_position(market_id="0xnyc", side="YES", shares_to_sell=Decimal("0.0001"))
        assert "minimum" in str(exc.value).lower()
        assert get_trade_history() == []

    def test_sell_without_live_price_rejected(self):
        """Tanpa snapshot harga, penjualan tidak boleh memakai harga karangan."""
        db = get_db_session()
        db.query(MarketSnapshot).filter(MarketSnapshot.market_id == "0xnyc").delete()
        db.commit()
        db.close()
        with pytest.raises(ValueError) as exc:
            sell_paper_position(market_id="0xnyc", side="YES")
        assert "tidak tersedia" in str(exc.value).lower()

    def test_sell_rejected_when_market_not_accepting_orders(self):
        add_snapshot("0xnyc", "0.60", name="Will NYC exceed 85°F?", status="closed")
        with pytest.raises(ValueError) as exc:
            sell_paper_position(market_id="0xnyc", side="YES")
        assert "tidak menerima order" in str(exc.value)

    def test_sell_applies_sell_side_slippage(self, monkeypatch):
        monkeypatch.setattr(settings, "SLIPPAGE_BPS", 100)  # 1%
        res = sell_paper_position(market_id="0xnyc", side="YES")
        assert res["exit_price"] == pytest.approx(0.594, abs=0.0001)  # 0.60 * (1 - 0.01)
        assert res["realized_pnl"] < 0

    def test_deposit_paper_funds(self):
        initial_bal = get_account_status()["balance"]
        deposit_paper_funds(Decimal("50.00"))
        assert get_account_status()["balance"] == initial_bal + Decimal("50.00")

    def test_api_sell_endpoint(self):
        res = sell_position_api(SellPositionRequest(market_id="0xnyc", side="YES"))
        assert res["success"] is True
        assert res["proceeds"] > 0

    def test_api_sell_endpoint_not_found_returns_404(self):
        with pytest.raises(HTTPException) as exc:
            sell_position_api(SellPositionRequest(market_id="0xhk", side="YES"))
        assert exc.value.status_code == 404

    def test_api_deposit_endpoint(self):
        res = deposit_funds_api(DepositFundsRequest(amount=25.0))
        assert res["success"] is True
        assert res["amount"] == 25.0

    def test_api_reset_endpoint(self):
        res = reset_account_api()
        assert res["success"] is True
        status = get_account_status()
        assert status["balance"] == Decimal("20.00")
        assert status["open_trades"] == 0
        assert get_trade_history() == []

    def test_api_search_markets_with_time_filter(self):
        res = search_markets_api(time_filter="24h", sort_by="ending_soonest")
        assert {m["market_id"] for m in res} == {"0xnyc", "0xhk"}
