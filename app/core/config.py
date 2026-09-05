"""
Configuration Loader menggunakan pydantic-settings.
Membaca environment variables dari file .env dengan tipe data tervalidasi.
"""
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Optional


def _resolve_database_url(url: str) -> str:
    """
    Jika aplikasi berjalan di dalam container Docker dan URL masih menunjuk ke
    localhost/127.0.0.1, otomatis dialihkan ke hostname service 'postgres' di docker network.
    """
    if os.path.exists("/.dockerenv") or os.getenv("IN_DOCKER"):
        if "@localhost" in url or "@127.0.0.1" in url:
            url = re.sub(r"@(localhost|127\.0\.0\.1)(:\d+)?/", "@postgres:5432/", url)
    return url


try:
    from pydantic import field_validator
    from pydantic_settings import BaseSettings, SettingsConfigDict

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
        MAX_POSITION_SIZE: Decimal = Decimal("1.00")
        INITIAL_BALANCE: Decimal = Decimal("20.00")
        PRICE_DIVERGENCE_WARNING_THRESHOLD: Decimal = Decimal("0.05")

        # Telegram Integration
        TELEGRAM_BOT_TOKEN: Optional[str] = None
        TELEGRAM_CHAT_ID: Optional[str] = None

        # Market Collector Configuration
        COLLECTOR_INTERVAL_SECONDS: int = 300
        GAMMA_API_BASE_URL: str = "https://gamma-api.polymarket.com"

        # Logging Configuration
        LOG_LEVEL: str = "INFO"
        LOG_FILE: str = "logs/app.log"
        APP_ENV: str = "development"

except ImportError:
    # Fallback jika pydantic-settings belum terinstall di virtualenv lokal
    from pydantic import BaseModel, Field

    def _load_env_file(filepath: Path):
        """Helper sederhana untuk parsing .env jika python-dotenv belum ada."""
        if not filepath.exists():
            return
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k not in os.environ:
                        os.environ[k] = v

    # Coba load dari .env di root atau current dir
    _load_env_file(Path(".env"))
    _load_env_file(Path(__file__).resolve().parent.parent.parent / ".env")

    class Settings(BaseModel):
        DATABASE_URL: str = Field(default_factory=lambda: _resolve_database_url(os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/paper_trading")))
        SLIPPAGE_BPS: int = Field(default_factory=lambda: int(os.getenv("SLIPPAGE_BPS", "0")))
        SPREAD_BPS: int = Field(default_factory=lambda: int(os.getenv("SPREAD_BPS", "0")))
        MAX_POSITION_SIZE: Decimal = Field(default_factory=lambda: Decimal(os.getenv("MAX_POSITION_SIZE", "1.00")))
        INITIAL_BALANCE: Decimal = Field(default_factory=lambda: Decimal(os.getenv("INITIAL_BALANCE", "20.00")))
        PRICE_DIVERGENCE_WARNING_THRESHOLD: Decimal = Field(default_factory=lambda: Decimal(os.getenv("PRICE_DIVERGENCE_WARNING_THRESHOLD", "0.05")))
        TELEGRAM_BOT_TOKEN: Optional[str] = Field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN"))
        TELEGRAM_CHAT_ID: Optional[str] = Field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID"))
        COLLECTOR_INTERVAL_SECONDS: int = Field(default_factory=lambda: int(os.getenv("COLLECTOR_INTERVAL_SECONDS", "300")))
        GAMMA_API_BASE_URL: str = Field(default_factory=lambda: os.getenv("GAMMA_API_BASE_URL", "https://gamma-api.polymarket.com"))
        LOG_LEVEL: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
        LOG_FILE: str = Field(default_factory=lambda: os.getenv("LOG_FILE", "logs/app.log"))
        APP_ENV: str = Field(default_factory=lambda: os.getenv("APP_ENV", "development"))


# Singleton instance untuk kemudahan import: `from app.core.config import settings`
settings = Settings()
