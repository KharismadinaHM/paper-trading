"""
Unit tests for Market Collector (Polymarket Weather markets).
"""
import io
import json
import urllib.error
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.market_collector.collector import (
    fetch_weather_markets,
    parse_market_dict,
    run_collection_cycle,
    save_snapshot,
    save_snapshots,
)
from app.paper_trading.models import Base, MarketSnapshot


@pytest.fixture
def in_memory_session():
    """Membuat in-memory SQLite database session untuk pengujian persistensi DB."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


class TestMarketParsing:
    """Pengujian logika parsing dan pencocokan eksplisit outcome Yes/No."""

    def test_parse_standard_order_yes_no(self):
        """Urutan standar: Yes di index 0, No di index 1."""
        raw = {
            "conditionId": "0xabc123",
            "question": "Will it rain in Seattle on Sep 5?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.72", "0.28"]',
            "closed": False,
            "endDate": "2026-09-05T20:00:00Z",
        }
        parsed = parse_market_dict(raw)
        assert parsed is not None
        assert parsed["market_id"] == "0xabc123"
        assert parsed["market_name"] == "Will it rain in Seattle on Sep 5?"
        assert parsed["price_yes"] == Decimal("0.72")
        assert parsed["price_no"] == Decimal("0.28")
        assert parsed["current_price"] == Decimal("0.72")
        assert parsed["status"] == "open"
        assert parsed["is_resolved"] is False
        assert parsed["category"] in ["Precipitation", "Weather"]
        assert parsed["resolution_time"] == datetime(2026, 9, 5, 20, 0, 0, tzinfo=timezone.utc)

    def test_parse_inverted_order_no_yes(self):
        """Urutan terbalik: No di index 0, Yes di index 1."""
        raw = {
            "conditionId": "0xdef456",
            "question": "Will NYC temperature exceed 90F?",
            "outcomes": '["No", "Yes"]',
            "outcomePrices": '["0.35", "0.65"]',
            "closed": False,
            "endDate": "2026-09-06T18:00:00Z",
        }
        parsed = parse_market_dict(raw)
        assert parsed is not None
        # price_yes harus tetap mengambil nilai Yes (0.65) meskipun Yes ada di index 1!
        assert parsed["price_yes"] == Decimal("0.65")
        # price_no harus tetap mengambil nilai No (0.35) meskipun No ada di index 0!
        assert parsed["price_no"] == Decimal("0.35")
        assert parsed["current_price"] == Decimal("0.65")

    def test_parse_case_insensitivity_and_whitespace(self):
        """Pencocokan Yes/No harus case-insensitive dan tahan spasi."""
        raw = {
            "conditionId": "0xcase789",
            "question": "Snowfall in Denver?",
            "outcomes": '["  yEs  ", "  nO  "]',
            "outcomePrices": '["0.775", "0.225"]',
            "closed": True,
            "endDate": "2026-09-04T12:00:00Z",
        }
        parsed = parse_market_dict(raw)
        assert parsed is not None
        assert parsed["price_yes"] == Decimal("0.775")
        assert parsed["price_no"] == Decimal("0.225")
        assert parsed["status"] == "resolved"
        assert parsed["is_resolved"] is True

    def test_parse_handles_malformed_json(self):
        """Jika string outcomes atau outcomePrices bukan JSON valid, tidak boleh crash."""
        raw = {
            "conditionId": "0xbad_json",
            "question": "Rain in Chicago?",
            "outcomes": "NOT_JSON",
            "outcomePrices": "{corrupt}",
            "closed": False,
        }
        parsed = parse_market_dict(raw)
        assert parsed is not None
        assert parsed["price_yes"] is None
        assert parsed["price_no"] is None
        assert parsed["current_price"] is None

    def test_parse_missing_mandatory_fields(self):
        """Jika tidak ada conditionId dan id, atau tidak ada question, kembalikan None."""
        assert parse_market_dict({"question": "Missing ID"}) is None
        assert parse_market_dict({"conditionId": "0x123"}) is None


class TestMarketFetching:
    """Pengujian fetch_weather_markets dengan mock API."""

    @patch("urllib.request.urlopen")
    def test_fetch_weather_markets_success(self, mock_urlopen):
        """Memastikan fetch memanggil API dan mem-parse outcomes & prices secara akurat."""
        api_payload = {
            "events": [
                {
                    "title": "NYC Weather Event",
                    "markets": [
                        {
                            "conditionId": "0xmkt_weather_1",
                            "question": "Will it rain in NYC?",
                            "outcomes": '["No", "Yes"]',
                            "outcomePrices": '["0.20", "0.80"]',
                            "closed": False,
                            "endDate": "2026-09-05T00:00:00Z",
                        }
                    ],
                }
            ]
        }

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_payload).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        results = fetch_weather_markets(queries=["weather"], active_only=True)
        assert len(results) >= 1
        mkt = next(m for m in results if m["market_id"] == "0xmkt_weather_1")
        assert mkt["market_name"] == "Will it rain in NYC?"
        assert mkt["price_yes"] == Decimal("0.80")
        assert mkt["price_no"] == Decimal("0.20")
        assert mkt["current_price"] == Decimal("0.80")

    @patch("urllib.request.urlopen")
    def test_fetch_resilient_to_network_failure(self, mock_urlopen):
        """Kegagalan koneksi API tidak boleh melempar exception atau mematikan proses."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        results = fetch_weather_markets(queries=["weather"])
        assert isinstance(results, list)
        assert len(results) == 0

    @patch("urllib.request.urlopen")
    def test_fetch_skips_bad_market_in_batch(self, mock_urlopen):
        """Market yang korup dalam sebuah event dilewati tanpa menggagalkan market valid lainnya."""
        api_payload = {
            "events": [
                {
                    "title": "Mixed Weather Markets",
                    "markets": [
                        {"corrupt": "data"},  # Bad record
                        {
                            "conditionId": "0xvalid_weather",
                            "question": "Will it snow in Boston?",
                            "outcomes": '["Yes", "No"]',
                            "outcomePrices": '["0.60", "0.40"]',
                            "closed": False,
                        },
                    ],
                }
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_payload).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        results = fetch_weather_markets(queries=["snow"], active_only=True)
        assert len(results) == 1
        assert results[0]["market_id"] == "0xvalid_weather"
        assert results[0]["price_yes"] == Decimal("0.60")


class TestSnapshotPersistence:
    """Pengujian perilaku penyimpanan database (timeseries snapshot INSERT)."""

    def test_save_snapshot_creates_new_row_on_every_call(self, in_memory_session):
        """Memanggil save_snapshot 2x dengan data market yang sama harus menghasilkan 2 rows terpisah di DB."""
        market_data = {
            "market_id": "0xcondition_time_series_1",
            "market_name": "Will it rain in Dallas?",
            "status": "open",
            "is_resolved": False,
            "resolution_time": datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc),
            "end_date": datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc),
            "price_yes": Decimal("0.70"),
            "price_no": Decimal("0.30"),
            "current_price": Decimal("0.70"),
            "category": "Weather",
        }

        # Simpan pemanggilan ke-1
        row1 = save_snapshot(market_data, session=in_memory_session)

        # Simpan pemanggilan ke-2 (misal data snapshot 5 menit kemudian)
        market_data_2 = dict(market_data)
        market_data_2["price_yes"] = Decimal("0.73")
        market_data_2["current_price"] = Decimal("0.73")
        row2 = save_snapshot(market_data_2, session=in_memory_session)

        # Query langsung dari tabel database
        all_snapshots = (
            in_memory_session.query(MarketSnapshot)
            .filter_by(market_id="0xcondition_time_series_1")
            .all()
        )

        assert len(all_snapshots) == 2, "Harus menghasilkan tepat 2 baris terpisah (bukan overwrite)"
        assert row1.id != row2.id, "Setiap baris snapshot harus memiliki primary key UUID yang unik"
        prices = [s.price_yes for s in all_snapshots]
        assert Decimal("0.700000") in prices or Decimal("0.70") in prices
        assert Decimal("0.730000") in prices or Decimal("0.73") in prices

    def test_save_snapshots_batch_insert(self, in_memory_session):
        """Batch save_snapshots menyimpan seluruh item sebagai baris baru."""
        batch = [
            {
                "market_id": f"0xbatch_{i}",
                "market_name": f"Market {i}",
                "price_yes": Decimal("0.50"),
                "price_no": Decimal("0.50"),
                "current_price": Decimal("0.50"),
            }
            for i in range(5)
        ]
        saved = save_snapshots(batch, session=in_memory_session)
        assert len(saved) == 5

        total_rows = in_memory_session.query(MarketSnapshot).count()
        assert total_rows == 5

    @patch("app.market_collector.collector.fetch_weather_markets")
    def test_run_collection_cycle(self, mock_fetch, in_memory_session):
        """Pengujian eksekusi run_collection_cycle dari fetch hingga persistensi."""
        mock_fetch.return_value = [
            {
                "market_id": "0xcycle_1",
                "market_name": "Cycle Market",
                "price_yes": Decimal("0.68"),
                "price_no": Decimal("0.32"),
                "current_price": Decimal("0.68"),
            }
        ]

        count = run_collection_cycle(session=in_memory_session)
        assert count == 1

        db_row = in_memory_session.query(MarketSnapshot).filter_by(market_id="0xcycle_1").first()
        assert db_row is not None
        assert db_row.market_name == "Cycle Market"
