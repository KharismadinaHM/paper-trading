"""
Test paginasi collector, retensi snapshot, dan validasi konfigurasi produksi.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from app.core.config import Settings, validate_production_settings, settings
from app.core.database import get_db_session
from app.market_collector.collector import fetch_weather_markets, prune_market_snapshots
from app.paper_trading.models import MarketSnapshot


def _event(i):
    return {"id": f"ev{i}", "title": f"Event {i}", "markets": [{
        "conditionId": f"0xm{i}", "question": f"Highest temperature {i}?",
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.40", "0.60"]', "closed": False,
    }]}


def _response(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    return resp


class TestCollectorPagination:

    def test_paginates_weather_tag_events_until_short_page(self, monkeypatch):
        monkeypatch.setattr(settings, "WEATHER_TAG_IDS", "84")
        pages = {0: [_event(i) for i in range(100)], 100: [_event(i) for i in range(100, 130)]}
        requested_offsets = []

        def fake_urlopen(req, timeout=None):
            url = urlparse(req.full_url)
            if url.path.endswith("/public-search"):
                return _response({"events": []})
            offset = int(parse_qs(url.query)["offset"][0])
            requested_offsets.append(offset)
            return _response(pages.get(offset, []))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            markets = fetch_weather_markets(queries=["weather"])

        assert requested_offsets == [0, 100]  # berhenti setelah halaman tidak penuh
        assert len(markets) == 130
        assert len({m["market_id"] for m in markets}) == 130

    def test_page_limit_is_respected(self, monkeypatch):
        monkeypatch.setattr(settings, "WEATHER_TAG_IDS", "84")
        monkeypatch.setattr(settings, "COLLECTOR_MAX_PAGES", 3)
        calls = []

        def fake_urlopen(req, timeout=None):
            url = urlparse(req.full_url)
            if url.path.endswith("/public-search"):
                return _response({"events": []})
            offset = int(parse_qs(url.query)["offset"][0])
            calls.append(offset)
            return _response([_event(offset + i) for i in range(100)])  # selalu penuh

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            fetch_weather_markets(queries=["weather"])
        assert calls == [0, 100, 200]


class TestSnapshotRetention:

    def _add(self, db, market_id, age_days):
        db.add(MarketSnapshot(
            id=uuid.uuid4(), market_id=market_id, market_name=market_id, status="open",
            is_resolved=False, price_yes=Decimal("0.5"), price_no=Decimal("0.5"),
            timestamp=datetime.now(timezone.utc) - timedelta(days=age_days),
        ))

    def test_prunes_old_rows_but_keeps_latest_per_market(self):
        db = get_db_session()
        try:
            for age in (60, 45, 1):
                self._add(db, "0xactive", age)
            for age in (90, 70):
                self._add(db, "0xdormant", age)  # tidak ada snapshot baru
            db.commit()

            deleted = prune_market_snapshots(retention_days=30, session=db)
            assert deleted == 3
            remaining = {(s.market_id, round((datetime.now(timezone.utc) - s.timestamp.replace(tzinfo=timezone.utc)).days))
                         for s in db.query(MarketSnapshot).all()}
            assert remaining == {("0xactive", 1), ("0xdormant", 70)}
        finally:
            db.close()

    def test_zero_retention_disables_pruning(self):
        db = get_db_session()
        try:
            self._add(db, "0xa", 400)
            self._add(db, "0xa", 300)
            db.commit()
            assert prune_market_snapshots(retention_days=0, session=db) == 0
            assert db.query(MarketSnapshot).count() == 2
        finally:
            db.close()


class TestProductionValidation:

    def _settings(self, **overrides):
        base = {
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql://postgres:Str0ng-Pass@db:5432/paper_trading",
            "DASHBOARD_PASSWORD": "dash-pass",
        }
        base.update(overrides)
        return Settings(_env_file=None, **base)

    def test_secure_production_config_passes(self):
        validate_production_settings(self._settings())

    def test_default_db_password_rejected(self):
        with pytest.raises(RuntimeError) as exc:
            validate_production_settings(
                self._settings(DATABASE_URL="postgresql://postgres:postgres@db:5432/paper_trading")
            )
        assert "password database" in str(exc.value)

    def test_missing_dashboard_password_rejected(self):
        with pytest.raises(RuntimeError) as exc:
            validate_production_settings(self._settings(DASHBOARD_PASSWORD=None))
        assert "DASHBOARD_PASSWORD" in str(exc.value)

    def test_development_is_not_enforced(self):
        validate_production_settings(self._settings(
            APP_ENV="development", DATABASE_URL="postgresql://postgres:postgres@localhost/x", DASHBOARD_PASSWORD=None,
        ))
