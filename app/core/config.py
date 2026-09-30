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

    # Rekomendasi market suhu berbasis jam puncak lokal (lihat app/paper_trading/weather_peaks.py)
    # Jam puncak per kota dihitung dari posisi matahari + kalibrasi riset (peak_calibration.json).
    # Jendela rekomendasi = [awal puncak - LEAD - WINDOW, awal puncak - LEAD) waktu setempat
    TEMP_PEAK_DURATION_HOURS: float = 1.0
    RECOMMENDATION_LEAD_HOURS: float = 1.0
    RECOMMENDATION_WINDOW_HOURS: float = 1.0
    # Jenis market yang direkomendasikan (dashboard & Telegram): "highest", "lowest", atau keduanya
    RECOMMENDATION_KINDS: str = "highest,lowest"
    # Bracket dianggap likuid jika selisih ask − bid order book ≤ nilai ini (0.10 = 10¢)
    RECOMMENDATION_MAX_SPREAD: float = 0.10
    # Jam puncak cadangan untuk kota tanpa koordinat (hanya ditambahkan lewat CITY_TIMEZONE_OVERRIDES)
    TEMP_HIGH_PEAK_HOUR: float = 14.0
    TEMP_LOW_PEAK_HOUR: float = 5.0
    # JSON opsional, AWAL jam puncak lokal per kota, mis. {"Hong Kong": {"highest": 14, "lowest": 6}}
    TEMP_PEAK_HOUR_OVERRIDES: str = ""
    # JSON opsional untuk kota baru / koreksi, mis. {"Lima": "America/Lima"}
    CITY_TIMEZONE_OVERRIDES: str = ""

    # Dashboard Authentication (HTTP Basic). Aktif jika DASHBOARD_PASSWORD diisi.
    DASHBOARD_USERNAME: str = "admin"
    DASHBOARD_PASSWORD: Optional[str] = None

    # Telegram Integration
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None
    # Kirim daftar rekomendasi saat kota masuk jendela menjelang jam puncak suhu
    TELEGRAM_RECOMMENDATION_ALERTS: bool = True
    # Hanya kota dengan total volume market suhu terbesar yang dikirim ke Telegram (0 = semua kota)
    TELEGRAM_RECOMMENDATION_TOP_CITIES: int = 7
    # Alert lonjakan suhu Hong Kong (data HKO per 10 menit)
    HKO_ALERTS: bool = True
    HKO_ALERT_SPIKE_DEGREES: float = 0.8      # kenaikan minimal (°C) dalam jendela di bawah
    HKO_ALERT_WINDOW_MINUTES: int = 30
    HKO_ALERT_COOLDOWN_MINUTES: int = 30      # jeda minimal antar alert lonjakan
    HKO_ALERT_HOURS: str = "7-19"             # jam lokal HK saat alert aktif

    # Wallet tracker Polymarket
    WALLET_STATS_DAYS: int = 30                # periode win rate & PnL
    WALLET_DISCOVERY_CATEGORY: str = "WEATHER" # kategori leaderboard sumber rekomendasi
    WALLET_DISCOVERY_CANDIDATES: int = 12      # jumlah wallet leaderboard yang dianalisis
    WALLET_DISCOVERY_MIN_RESOLVED: int = 15    # minimal posisi selesai agar win rate bermakna
    WALLET_DISCOVERY_REFRESH_HOURS: int = 6
    WALLET_ALERTS: bool = True                 # alert transaksi wallet yang diikuti
    WALLET_POLL_SECONDS: int = 60
    WALLET_ALERT_MIN_USDC: float = 5.0         # abaikan transaksi lebih kecil dari ini
    WALLET_ALERT_WEATHER_ONLY: bool = False    # hanya alert transaksi market cuaca

    # Portfolio Polymarket sendiri (READ-ONLY). Alamat wallet = alamat di profil Polymarket.
    POLYMARKET_WALLET_ADDRESS: Optional[str] = None
    # Opsional, untuk saldo cash & open order (GET saja). Jangan pernah isi private key.
    POLYMARKET_API_KEY: Optional[str] = None
    POLYMARKET_API_SECRET: Optional[str] = None
    POLYMARKET_API_PASSPHRASE: Optional[str] = None
    POLYMARKET_SIGNER_ADDRESS: Optional[str] = None  # alamat pembuat API key (default = wallet address)
    POLYMARKET_SIGNATURE_TYPE: int = 1               # 1 = akun email/Magic, 2 = browser wallet, 0 = EOA
    # Zona waktu jam di notifikasi (default WIB)
    NOTIFY_TIMEZONE: str = "Asia/Jakarta"
    NOTIFY_TIMEZONE_LABEL: str = "WIB"

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
