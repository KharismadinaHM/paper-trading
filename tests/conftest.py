"""
Fixture global: setiap test memakai database SQLite in-memory yang terisolasi
dan notifikasi Telegram dinonaktifkan (tidak pernah mengirim pesan sungguhan).
"""
import urllib.error
import urllib.request
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.database as database
from app.core.config import settings


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", session_factory)
    database.init_db(bind=engine)
    # Cache modul yang berisi data database tidak boleh terbawa ke test lain
    from app.paper_trading import autotrader, live_market_data
    autotrader._config_cache.update(at=0.0, values={})
    live_market_data.clear_cache()
    yield engine
    engine.dispose()
    autotrader._config_cache.update(at=0.0, values={})


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    """Test tidak boleh mengakses Gamma API sungguhan; test yang butuh data harus mem-patch urlopen."""
    def _blocked(*args, **kwargs):
        raise urllib.error.URLError("Akses jaringan diblokir di test (patch urllib.request.urlopen)")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)


@pytest.fixture(autouse=True)
def no_external_side_effects(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", None)
    monkeypatch.setattr(settings, "DASHBOARD_PASSWORD", None)
    monkeypatch.setattr("app.paper_service._notify_async", lambda *args, **kwargs: None)
    # Nilai risk default yang deterministik (tidak bergantung pada isi .env lokal)
    monkeypatch.setattr(settings, "SLIPPAGE_BPS", 0)
    monkeypatch.setattr(settings, "SPREAD_BPS", 0)
    monkeypatch.setattr(settings, "FEE_RATE_BPS", 0)
    monkeypatch.setattr(settings, "INITIAL_BALANCE", Decimal("20.00"))
    monkeypatch.setattr(settings, "MAX_POSITION_SIZE", Decimal("1.00"))
    monkeypatch.setattr(settings, "MAX_EXPOSURE_PER_MARKET", Decimal("1.00"))
    monkeypatch.setattr(settings, "MAX_TOTAL_EXPOSURE", Decimal("10.00"))
    monkeypatch.setattr(settings, "REJECT_STALE_ORDERS", True)
    monkeypatch.setattr(settings, "ALLOW_SYNTHETIC_MARKETS", False)
