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


class TestMarketLatestAndHistory:

    # Waktu resolusi tetap (tidak ikut bergeser dengan waktu observasi)
    BASE = datetime.now(timezone.utc).replace(microsecond=0)

    def _market(self, market_id="0xm", price="0.40", hours=5, now=None):
        now = now or datetime.now(timezone.utc)
        return {
            "market_id": market_id, "market_name": f"Market {market_id}", "category": "Temperature",
            "status": "open", "is_resolved": False, "resolution_time": self.BASE + timedelta(hours=hours),
            "end_date": self.BASE + timedelta(hours=hours), "price_yes": Decimal(price),
            "price_no": Decimal("1") - Decimal(price), "current_price": Decimal(price), "timestamp": now,
        }

    def _counts(self):
        from app.paper_trading.models import MarketLatest
        db = get_db_session()
        try:
            return db.query(MarketSnapshot).count(), db.query(MarketLatest).count()
        finally:
            db.close()

    def test_unchanged_price_refreshes_latest_without_new_history(self):
        from app.market_collector.collector import record_market_observations
        from app.paper_service import get_market_by_id
        t0 = datetime.now(timezone.utc) - timedelta(minutes=20)
        assert record_market_observations([self._market(now=t0)], now=t0)["history_rows"] == 1
        t1 = t0 + timedelta(minutes=5)
        assert record_market_observations([self._market(now=t1)], now=t1)["history_rows"] == 0
        assert self._counts() == (1, 1)
        # waktu observasi terbaru tetap dipakai untuk cek stale
        assert get_market_by_id("0xm")["timestamp"].replace(tzinfo=timezone.utc) == t1

    def test_price_change_writes_history(self):
        from app.market_collector.collector import record_market_observations
        from app.paper_service import get_market_by_id
        t0 = datetime.now(timezone.utc)
        record_market_observations([self._market(now=t0)], now=t0)
        t1 = t0 + timedelta(minutes=5)
        assert record_market_observations([self._market(price="0.45", now=t1)], now=t1)["history_rows"] == 1
        assert self._counts() == (2, 1)
        assert get_market_by_id("0xm")["price_yes"] == Decimal("0.45")

    def test_heartbeat_writes_history_for_unchanged_price(self, monkeypatch):
        from app.market_collector.collector import record_market_observations
        monkeypatch.setattr(settings, "SNAPSHOT_HEARTBEAT_SECONDS", 3600)
        t0 = datetime.now(timezone.utc) - timedelta(hours=2)
        record_market_observations([self._market(now=t0)], now=t0)
        t1 = t0 + timedelta(minutes=61)
        assert record_market_observations([self._market(now=t1)], now=t1)["history_rows"] == 1

    def test_collection_cycle_skips_markets_past_resolution(self):
        from app.market_collector.collector import run_collection_cycle
        now = datetime.now(timezone.utc)
        markets = [self._market("0xfuture", now=now), self._market("0xpast", hours=-2, now=now)]
        with patch("app.market_collector.collector.fetch_all_markets", return_value=markets):
            assert run_collection_cycle(now=now) == 1
        from app.paper_service import get_market_by_id
        assert get_market_by_id("0xpast") is None

    def test_latest_ignores_older_out_of_order_snapshot(self):
        from app.paper_service import get_market_by_id
        now = datetime.now(timezone.utc)
        db = get_db_session()
        for age, price in ((0, "0.70"), (10, "0.50")):  # insert terbaru dulu, lalu yang lebih tua
            db.add(MarketSnapshot(id=uuid.uuid4(), market_id="0xo", market_name="O", status="open",
                                  is_resolved=False, price_yes=Decimal(price), price_no=1 - Decimal(price),
                                  timestamp=now - timedelta(minutes=age)))
            db.commit()
        db.close()
        assert get_market_by_id("0xo")["price_yes"] == Decimal("0.70")

    def test_init_db_backfills_latest_from_existing_history(self, isolated_database):
        from app.core.database import init_db
        from app.paper_trading.models import MarketLatest
        now = datetime.now(timezone.utc)
        with isolated_database.begin() as conn:  # histori lama tanpa market_latest (sebelum upgrade)
            for age, price in ((30, 0.40), (5, 0.55)):
                conn.execute(MarketSnapshot.__table__.insert().values(
                    id=uuid.uuid4(), market_id="0xold", market_name="Old", status="open", is_resolved=False,
                    price_yes=price, price_no=1 - price, timestamp=now - timedelta(minutes=age)))
            conn.execute(MarketLatest.__table__.delete())
        init_db(bind=isolated_database)
        db = get_db_session()
        try:
            row = db.get(MarketLatest, "0xold")
            assert row is not None and row.price_yes == Decimal("0.55")
        finally:
            db.close()

    def test_prune_removes_long_unseen_latest_rows_without_positions(self):
        from app.market_collector.collector import record_market_observations
        from app.paper_trading.models import MarketLatest
        old = datetime.now(timezone.utc) - timedelta(days=40)
        record_market_observations([self._market("0xgone", now=old)], now=old)
        record_market_observations([self._market("0xfresh")])
        prune_market_snapshots(retention_days=30)
        db = get_db_session()
        try:
            assert {r.market_id for r in db.query(MarketLatest)} == {"0xfresh"}
        finally:
            db.close()
