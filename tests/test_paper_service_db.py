"""
Unit tests for database-backed market query functions in app/paper_service.py.
Menguji get_market_by_id(), get_market_snapshots(), freshness check, dan anti-stale behavior.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.paper_service import (
    create_paper_order,
    get_market_by_id,
    get_market_snapshots,
    reset_paper_account,
)
from app.paper_trading.models import Base, MarketSnapshot


@pytest.fixture
def db_session():
    """Membuat SQLite in-memory database session dengan skema tabel lengkap."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


class TestPaperServiceDatabaseQueries:

    def test_get_market_by_id_returns_latest_snapshot(self, db_session):
        """Memastikan get_market_by_id selalu mengambil snapshot dengan timestamp paling baru."""
        now = datetime.now(timezone.utc)
        market_id = "0xweather_test_1"

        # Snapshot #1: 10 menit lalu, harga 0.60
        s1 = MarketSnapshot(
            id=uuid.uuid4(),
            market_id=market_id,
            market_name="Rain in Seattle?",
            status="open",
            is_resolved=False,
            price_yes=Decimal("0.60"),
            price_no=Decimal("0.40"),
            current_price=Decimal("0.60"),
            timestamp=now - timedelta(minutes=10),
        )
        # Snapshot #2: 2 menit lalu, harga 0.72 (TERBARU)
        s2 = MarketSnapshot(
            id=uuid.uuid4(),
            market_id=market_id,
            market_name="Rain in Seattle?",
            status="open",
            is_resolved=False,
            price_yes=Decimal("0.72"),
            price_no=Decimal("0.28"),
            current_price=Decimal("0.72"),
            timestamp=now - timedelta(minutes=2),
        )
        db_session.add_all([s1, s2])
        db_session.commit()

        result = get_market_by_id(market_id, now=now, db=db_session)
        assert result is not None
        assert result["market_id"] == market_id
        # Harus merefleksikan snapshot terbaru (s2)
        assert result["price_yes"] == Decimal("0.72")
        assert result["current_price"] == Decimal("0.72")
        assert result["timestamp"] == s2.timestamp
        assert result["is_stale"] is False

    def test_get_market_by_id_not_found_returns_none(self, db_session):
        """Jika market_id tidak ditemukan, kembalikan None tanpa crash/exception."""
        res = get_market_by_id("non_existent_market", db=db_session)
        assert res is None

    def test_freshness_is_stale_flag(self, db_session):
        """Snapshot yang lebih tua dari 15 menit ditandai is_stale=True."""
        now = datetime.now(timezone.utc)
        market_stale_id = "0xstale_market"
        market_fresh_id = "0xfresh_market"

        # Snapshot tua: 20 menit lalu (> 15 menit threshold)
        s_stale = MarketSnapshot(
            id=uuid.uuid4(),
            market_id=market_stale_id,
            market_name="Old weather market",
            status="open",
            price_yes=Decimal("0.50"),
            price_no=Decimal("0.50"),
            timestamp=now - timedelta(minutes=20),
        )
        # Snapshot segar: 5 menit lalu (< 15 menit threshold)
        s_fresh = MarketSnapshot(
            id=uuid.uuid4(),
            market_id=market_fresh_id,
            market_name="Fresh weather market",
            status="open",
            price_yes=Decimal("0.50"),
            price_no=Decimal("0.50"),
            timestamp=now - timedelta(minutes=5),
        )
        db_session.add_all([s_stale, s_fresh])
        db_session.commit()

        res_stale = get_market_by_id(market_stale_id, now=now, db=db_session)
        assert res_stale is not None
        assert res_stale["is_stale"] is True

        res_fresh = get_market_by_id(market_fresh_id, now=now, db=db_session)
        assert res_fresh is not None
        assert res_fresh["is_stale"] is False

    def test_get_market_snapshots_returns_only_latest_per_market(self, db_session):
        """Memastikan get_market_snapshots mengambil HANYA snapshot terbaru per market_id."""
        now = datetime.now(timezone.utc)

        # Market A: 2 snapshot
        s_a1 = MarketSnapshot(
            id=uuid.uuid4(),
            market_id="mkt_A",
            market_name="Market A",
            status="open",
            price_yes=Decimal("0.30"),
            timestamp=now - timedelta(minutes=10),
        )
        s_a2 = MarketSnapshot(
            id=uuid.uuid4(),
            market_id="mkt_A",
            market_name="Market A",
            status="open",
            price_yes=Decimal("0.35"),
            timestamp=now - timedelta(minutes=1),
        )

        # Market B: 1 snapshot
        s_b = MarketSnapshot(
            id=uuid.uuid4(),
            market_id="mkt_B",
            market_name="Market B",
            status="open",
            price_yes=Decimal("0.50"),
            timestamp=now - timedelta(minutes=5),
        )

        db_session.add_all([s_a1, s_a2, s_b])
        db_session.commit()

        snapshots = get_market_snapshots(now=now, db=db_session)
        # Total harus 2 market unik, BUKAN 3 row histori
        assert len(snapshots) == 2

        map_by_id = {s["market_id"]: s for s in snapshots}
        assert "mkt_A" in map_by_id
        assert "mkt_B" in map_by_id
        # Market A harus mengambil snapshot terbaru (s_a2 dengan price_yes 0.35)
        assert map_by_id["mkt_A"]["price_yes"] == Decimal("0.35")

    def test_get_market_snapshots_skips_resolved_markets(self, db_session):
        """Secara default mengecualikan market yang is_resolved=True atau status='resolved'."""
        now = datetime.now(timezone.utc)

        open_m = MarketSnapshot(
            id=uuid.uuid4(),
            market_id="mkt_open",
            market_name="Open Market",
            status="open",
            is_resolved=False,
            price_yes=Decimal("0.60"),
            timestamp=now,
        )
        resolved_m = MarketSnapshot(
            id=uuid.uuid4(),
            market_id="mkt_resolved",
            market_name="Resolved Market",
            status="resolved",
            is_resolved=True,
            price_yes=Decimal("1.00"),
            timestamp=now,
        )
        db_session.add_all([open_m, resolved_m])
        db_session.commit()

        default_list = get_market_snapshots(now=now, include_resolved=False, db=db_session)
        assert len(default_list) == 1
        assert default_list[0]["market_id"] == "mkt_open"

        all_list = get_market_snapshots(now=now, include_resolved=True, db=db_session)
        assert len(all_list) == 2

    def test_create_order_emits_warning_on_stale_market(self, db_session):
        """Memastikan create_paper_order mencatat warning jika market berstatus stale (>15m)."""
        reset_paper_account()
        now = datetime.now(timezone.utc)
        mkt_id = "0xstale_order_market"

        # Simpan snapshot 30 menit lalu
        old_snap = MarketSnapshot(
            id=uuid.uuid4(),
            market_id=mkt_id,
            market_name="Stale Test Market",
            status="open",
            is_resolved=False,
            price_yes=Decimal("0.70"),
            price_no=Decimal("0.30"),
            current_price=Decimal("0.70"),
            timestamp=now - timedelta(minutes=30),
        )
        db_session.add(old_snap)
        db_session.commit()

        # Patch get_db_session agar get_market_by_id menggunakan session ini
        from unittest.mock import patch
        with patch("app.paper_service.get_db_session", return_value=db_session):
            order = create_paper_order(
                market_id=mkt_id,
                side="YES",
                position_size=Decimal("1.00"),
                user_viewed_price=Decimal("0.70"),
                now=now,
            )

            assert order["status"] == "OPEN"
            assert order["warning"] is not None
            assert "stale" in order["warning"].lower()
