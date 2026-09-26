"""
Test regresi untuk state akun berbasis database: konsistensi saldo, risk limit,
settlement otomatis, dan autentikasi dashboard.
"""
import os
import threading
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.core.database as database
from app.core.config import settings
from app.core.database import get_db_session
from app.market_collector.collector import determine_winning_outcome, sync_markets_by_condition_ids
from app.paper_service import (
    create_paper_order,
    deposit_paper_funds,
    get_account_status,
    get_open_positions,
    get_performance,
    get_trade_history,
    reset_paper_account,
    sell_paper_position,
    settle_resolved_positions,
)
from app.paper_trading.models import MarketResolution, PaperPosition
from tests.test_positions_and_sell import add_snapshot


def resolve(market_id: str, outcome: str) -> None:
    db = get_db_session()
    db.add(MarketResolution(market_id=market_id, market_name=market_id, winning_outcome=outcome))
    db.commit()
    db.close()


class TestAccountConsistency:

    def test_new_account_equals_initial_balance(self):
        """Regresi: akun baru tidak boleh punya posisi/P&L bawaan (dulu $24.50 dari modal $20)."""
        status = get_account_status()
        assert status["balance"] == Decimal("20.0000")
        assert status["portfolio_value"] == Decimal("20.0000")
        assert status["realized_pnl"] == Decimal("0")
        assert status["total_pnl"] == Decimal("0")
        assert status["win_rate"] == Decimal("0")
        assert get_open_positions() == []
        assert get_trade_history() == []

    def test_state_persists_in_database(self):
        """Posisi tersimpan di tabel paper_positions (bertahan setelah restart proses)."""
        add_snapshot("0xa", "0.50")
        create_paper_order("0xa", "YES", Decimal("1.00"))
        db = get_db_session()
        try:
            assert db.query(PaperPosition).count() == 1
        finally:
            db.close()

    def test_status_and_performance_agree(self):
        add_snapshot("0xa", "0.50")
        create_paper_order("0xa", "YES", Decimal("1.00"))
        add_snapshot("0xa", "0.60")
        sell_paper_position("0xa", "YES")
        status, perf = get_account_status(), get_performance()
        assert status["realized_pnl"].quantize(Decimal("0.01")) == perf["realized_pnl"]
        assert status["win_rate"].quantize(Decimal("0.01")) == perf["win_rate"] == Decimal("1.00")
        assert perf["trades"] == 1

    def test_portfolio_value_is_cash_plus_positions(self):
        add_snapshot("0xa", "0.50")
        create_paper_order("0xa", "YES", Decimal("1.00"))
        add_snapshot("0xa", "0.80")
        status = get_account_status()
        assert status["balance"] == Decimal("19.0000")
        assert status["positions_value"] == Decimal("1.6000")
        assert status["portfolio_value"] == Decimal("20.6000")
        assert status["total_pnl"] == Decimal("0.6000")

    def test_deposit_increases_roi_basis(self):
        """Regresi: ROI dihitung dari total modal disetor, bukan hanya saldo awal."""
        add_snapshot("0xa", "0.50")
        create_paper_order("0xa", "YES", Decimal("1.00"))
        add_snapshot("0xa", "0.60")
        sell_paper_position("0xa", "YES")  # +0.20 realized
        assert get_account_status()["roi_pct"] == Decimal("1.00")  # 0.20 / 20
        deposit_paper_funds(Decimal("80.00"))
        status = get_account_status()
        assert status["total_capital"] == Decimal("100.0000")
        assert status["roi_pct"] == Decimal("0.20")  # 0.20 / 100

    def test_reset_clears_everything(self):
        add_snapshot("0xa", "0.50")
        create_paper_order("0xa", "YES", Decimal("1.00"))
        deposit_paper_funds(Decimal("5"))
        reset_paper_account()
        status = get_account_status()
        assert status["portfolio_value"] == Decimal("20.0000")
        assert status["total_capital"] == Decimal("20.0000")
        assert get_open_positions() == []


class TestOrderRules:

    def test_market_not_accepting_orders_rejected(self):
        """Market yang ditutup untuk order (acceptingOrders=false) harus ditolak."""
        add_snapshot("0xpaused", "0.50", status="closed")
        with pytest.raises(ValueError) as exc:
            create_paper_order("0xpaused", "YES", Decimal("1.00"))
        assert "tidak menerima order" in str(exc.value)

    def test_open_market_past_end_date_still_tradable(self):
        """Regresi: endDate Polymarket bukan batas trading; market yang masih open tetap bisa di-order."""
        add_snapshot("0xafter_end", "0.50", hours_to_resolution=-3)
        order = create_paper_order("0xafter_end", "YES", Decimal("1.00"))
        assert order["status"] == "FILLED"

    def test_cumulative_market_exposure_limit(self):
        """Regresi: MAX_EXPOSURE_PER_MARKET berlaku akumulatif, bukan per order."""
        add_snapshot("0xa", "0.50")
        create_paper_order("0xa", "YES", Decimal("0.60"))
        with pytest.raises(ValueError) as exc:
            create_paper_order("0xa", "YES", Decimal("0.60"))
        assert "Market exposure" in str(exc.value)
        assert get_account_status()["balance"] == Decimal("19.4000")

    def test_total_exposure_limit(self, monkeypatch):
        monkeypatch.setattr(settings, "MAX_TOTAL_EXPOSURE", Decimal("1.50"))
        add_snapshot("0xa", "0.50")
        add_snapshot("0xb", "0.50")
        create_paper_order("0xa", "YES", Decimal("1.00"))
        with pytest.raises(ValueError) as exc:
            create_paper_order("0xb", "YES", Decimal("1.00"))
        assert "Total exposure" in str(exc.value)

    def test_invalid_side_rejected(self):
        add_snapshot("0xa", "0.50")
        with pytest.raises(ValueError):
            create_paper_order("0xa", "maybe", Decimal("1.00"))

    @pytest.fixture
    def pooled_database(self, tmp_path, monkeypatch):
        """
        Engine dengan pool koneksi sungguhan (bukan satu koneksi bersama) agar thread
        benar-benar berjalan paralel. Set TEST_DATABASE_URL untuk menguji di PostgreSQL.
        """
        url = os.getenv("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'concurrency.db'}"
        kwargs = {"connect_args": {"timeout": 30}} if url.startswith("sqlite") else {"pool_size": 20}
        engine = create_engine(url, **kwargs)
        database.init_db(bind=engine)
        monkeypatch.setattr(database, "engine", engine)
        monkeypatch.setattr(database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine))
        reset_paper_account()
        yield engine
        engine.dispose()

    def test_concurrent_orders_keep_balance_consistent(self, monkeypatch, pooled_database):
        monkeypatch.setattr(settings, "MAX_EXPOSURE_PER_MARKET", Decimal("100"))
        monkeypatch.setattr(settings, "MAX_TOTAL_EXPOSURE", Decimal("100"))
        monkeypatch.setattr(settings, "MAX_POSITION_SIZE", Decimal("1.00"))
        add_snapshot("0xa", "0.50")
        errors = []

        def worker():
            try:
                create_paper_order("0xa", "YES", Decimal("1.00"))
            except Exception as err:  # pragma: no cover - dicatat untuk assertion
                errors.append(err)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        status = get_account_status()
        assert status["balance"] == Decimal("0.0000")
        assert status["invested"] == Decimal("20.0000")
        with pytest.raises(ValueError) as exc:
            create_paper_order("0xa", "YES", Decimal("1.00"))
        assert "Insufficient balance" in str(exc.value)


class TestSettlement:

    def setup_method(self):
        reset_paper_account()
        add_snapshot("0xw", "0.50", name="Will it rain?")
        create_paper_order("0xw", "YES", Decimal("1.00"))  # 2 shares @ 0.50

    def test_winning_position_pays_one_dollar_per_share(self):
        resolve("0xw", "YES")
        results = settle_resolved_positions()
        assert len(results) == 1 and results[0]["status"] == "WON"
        status = get_account_status()
        assert status["balance"] == Decimal("21.0000")  # 19 + 2 shares x $1
        assert status["realized_pnl"] == Decimal("1.0000")
        assert get_open_positions() == []
        assert get_trade_history()[0]["status"] == "WON"

    def test_losing_position_pays_zero(self):
        resolve("0xw", "NO")
        settle_resolved_positions()
        status = get_account_status()
        assert status["balance"] == Decimal("19.0000")
        assert status["realized_pnl"] == Decimal("-1.0000")
        assert get_trade_history()[0]["status"] == "LOST"

    def test_fee_charged_on_profit(self, monkeypatch):
        monkeypatch.setattr(settings, "FEE_RATE_BPS", 1000)  # 10% dari profit
        resolve("0xw", "YES")
        settle_resolved_positions()
        status = get_account_status()
        assert status["balance"] == Decimal("20.9000")
        assert status["total_fees"] == Decimal("0.1000")

    def test_invalid_market_refunds_stake(self):
        resolve("0xw", "INVALID")
        settle_resolved_positions()
        assert get_account_status()["balance"] == Decimal("20.0000")
        assert get_trade_history()[0]["status"] == "CANCELLED"

    def test_settlement_is_idempotent(self):
        resolve("0xw", "YES")
        settle_resolved_positions()
        assert settle_resolved_positions() == []
        assert len(get_trade_history()) == 1
        assert get_account_status()["balance"] == Decimal("21.0000")

    def test_sell_after_resolution_rejected(self):
        resolve("0xw", "YES")
        with pytest.raises(ValueError) as exc:
            sell_paper_position("0xw", "YES")
        assert "resolve" in str(exc.value)

    def test_open_position_valued_at_final_payout_before_settlement(self):
        resolve("0xw", "YES")
        pos = get_open_positions()[0]
        assert pos["price_source"] == "resolved"
        assert pos["current_value"] == Decimal("2.0000")


class TestResolutionDetection:

    def _raw(self, prices, closed=True, uma="resolved"):
        return {"conditionId": "0xr", "question": "Q?", "closed": closed, "umaResolutionStatus": uma,
                "outcomes": '["Yes", "No"]', "outcomePrices": prices}

    def test_yes_winner(self):
        assert determine_winning_outcome(self._raw('["1", "0"]')) == "YES"

    def test_no_winner_with_inverted_outcome_order(self):
        raw = self._raw('["1", "0"]')
        raw["outcomes"] = '["No", "Yes"]'
        assert determine_winning_outcome(raw) == "NO"

    def test_invalid_market(self):
        assert determine_winning_outcome(self._raw('["0.5", "0.5"]')) == "INVALID"

    def test_not_final_yet(self):
        assert determine_winning_outcome(self._raw('["0.97", "0.03"]')) is None
        assert determine_winning_outcome(self._raw('["1", "0"]', closed=False)) is None
        assert determine_winning_outcome(self._raw('["1", "0"]', uma="proposed")) is None

    def test_sync_records_resolution_and_enables_settlement(self, monkeypatch):
        reset_paper_account()
        add_snapshot("0xr", "0.50")
        create_paper_order("0xr", "NO", Decimal("1.00"))
        monkeypatch.setattr(
            "app.market_collector.collector.fetch_markets_by_condition_ids",
            lambda ids, **kw: [self._raw('["0", "1"]')],
        )
        assert sync_markets_by_condition_ids(["0xr"]) == {"snapshots": 1, "resolutions": 1}
        results = settle_resolved_positions()
        assert results[0]["status"] == "WON"
        assert get_account_status()["balance"] == Decimal("21.0000")


class TestDashboardAuth:

    def test_auth_required_when_password_set(self, monkeypatch):
        monkeypatch.setattr(settings, "DASHBOARD_PASSWORD", "s3cret")
        from app.dashboard import app
        client = TestClient(app)
        assert client.post("/api/account/reset").status_code == 401
        assert client.get("/api/summary").status_code == 401
        assert client.get("/api/summary", auth=("admin", "wrong")).status_code == 401
        assert client.get("/api/summary", auth=("admin", "s3cret")).status_code == 200

    def test_healthz_is_public(self, monkeypatch):
        monkeypatch.setattr(settings, "DASHBOARD_PASSWORD", "s3cret")
        from app.dashboard import app
        res = TestClient(app).get("/healthz")
        assert res.status_code == 200
        assert res.json()["database"] == "ok"

    def test_dashboard_page_renders(self):
        from app.dashboard import app
        add_snapshot("0xa", "0.50")
        create_paper_order("0xa", "YES", Decimal("1.00"))
        res = TestClient(app).get("/")
        assert res.status_code == 200
        assert "Market 0xa" in res.text
