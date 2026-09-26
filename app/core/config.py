"""
Configuration Loader menggunakan pydantic-settings.
Membaca environment variables dari file .env dengan tipe data tervalidasi.
"""
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_database_url(url: str) -> str:
    """
    Jika aplikasi berjalan di dalam container Docker dan URL masih menunjuk ke
    localhost/127.0.0.1, otomatis dialihkan ke hostname service 'postgres' di docker network.
    """
    if os.path.exists("/.dockerenv") or os.getenv("IN_DOCKER"):
        if "@localhost" in url or "@127.0.0.1" in url:
            url = re.sub(r"@(localhost|127\.0\.0\.1)(:\d+)?/", "@postgres:5432/", url)
    return url



class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", str(Path(__file__).resolve().parent.parent.parent / ".env")),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database Configuration
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/paper_trading"

    @field_validator("DATABASE_URL", mode="after")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        return _resolve_database_url(v)

    # Paper Trading & Risk Controls
    SLIPPAGE_BPS: int = 0
    SPREAD_BPS: int = 0
    FEE_RATE_BPS: int = 0
    MAX_POSITION_SIZE: Decimal = Decimal("1.00")
    # Batas eksposur akumulatif (cost basis posisi terbuka) per market dan total akun
    MAX_EXPOSURE_PER_MARKET: Decimal = Decimal("1.00")
    MAX_TOTAL_EXPOSURE: Decimal = Decimal("10.00")
    # Nilai minimum proceeds untuk order jual (menghindari trade $0.00)
    MIN_ORDER_NOTIONAL: Decimal = Decimal("0.01")
    INITIAL_BALANCE: Decimal = Decimal("20.00")
    PRICE_DIVERGENCE_WARNING_THRESHOLD: Decimal = Decimal("0.05")
    # Tolak order jika snapshot harga lebih tua dari 3x interval collector
    REJECT_STALE_ORDERS: bool = True

    # Dashboard Authentication (HTTP Basic). Aktif jika DASHBOARD_PASSWORD diisi.
    DASHBOARD_USERNAME: str = "admin"
    DASHBOARD_PASSWORD: Optional[str] = None

    # Telegram Integration
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None

    # Market Collector Configuration
    COLLECTOR_INTERVAL_SECONDS: int = 300
    GAMMA_API_BASE_URL: str = "https://gamma-api.polymarket.com"
    # Kategori market yang dikumpulkan (lihat app/market_collector/categories.py)
    ENABLED_MARKET_CATEGORIES: str = "weather,elon_tweets"
    # Tag Gamma API untuk event cuaca (Weather, Daily Temperature, Highest Temperature)
    WEATHER_TAG_IDS: str = "84,103040,104596"
    # Tag Gamma API untuk market jumlah tweet ("Tweet Markets"; difilter ke judul berisi "Elon")
    ELON_TWEETS_TAG_IDS: str = "972"
    # Batas halaman (x100 event) per tag pada setiap siklus collector
    COLLECTOR_MAX_PAGES: int = 10
    # Hapus snapshot lebih tua dari N hari (snapshot terbaru tiap market selalu disimpan). 0 = nonaktif
    SNAPSHOT_RETENTION_DAYS: int = 30
    # Baris histori market_snapshots ditulis saat harga/status berubah, atau minimal setiap N detik
    SNAPSHOT_HEARTBEAT_SECONDS: int = 3600
    # Market baseline sintetis hanya boleh dimuat secara eksplisit (untuk demo/dev lokal)
    ALLOW_SYNTHETIC_MARKETS: bool = False

    # Logging Configuration
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "logs/app.log"
    APP_ENV: str = "development"


DEFAULT_DB_PASSWORDS = {"postgres", "password", "changeme", ""}


def validate_production_settings(config: "Settings") -> None:
    """
    Fail-fast saat APP_ENV=production: tolak start jika password database masih default
    atau dashboard tidak dilindungi password.
    """
    if config.APP_ENV.strip().lower() != "production":
        return
    from sqlalchemy.engine import make_url

    problems = []
    url = make_url(config.DATABASE_URL)
    if url.get_backend_name() == "postgresql" and (url.password or "") in DEFAULT_DB_PASSWORDS:
        problems.append("password database (DATABASE_URL / POSTGRES_PASSWORD) masih default")
    if not config.DASHBOARD_PASSWORD:
        problems.append("DASHBOARD_PASSWORD belum diatur")
    if problems:
        raise RuntimeError("Konfigurasi produksi tidak aman: " + "; ".join(problems) + ".")


# Singleton instance untuk kemudahan import: `from app.core.config import settings`
settings = Settings()
