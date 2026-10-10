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
    # Max HK tidak dianggap final sebelum jam ini (HKT), kecuali suhu sudah turun ≥ HKO_FINAL_DROP dari max.
    # Kasus 30 Sep: pasar memberi 33°C 98.5¢ pukul 14:40, lalu HKO naik ke 34.2°C sekitar 15:50.
    HKO_FINAL_HOUR: int = 17
    HKO_FINAL_DROP: float = 1.0
    # Proyeksi per jam (/hk jam): bias (HKO − model) dipakai penuh s.d. 1 jam ke depan, lalu meluruh
    # linier sampai tersisa fraksi ini pada jam ke-6 (1.0 = tanpa peluruhan). Kalibrasi:
    # scripts/calibrate_hko_bias_decay.py
    HKO_BIAS_DECAY_AT_6H: float = 0.5
    # Alert "mendekati derajat berikutnya": max ≥ X + fraksi ini (mis. 33.7 → risiko bracket 34°C)
    HKO_NEAR_DEGREE_FRACTION: float = 0.7
    # "Waspada berbalik": bracket favorit ≥ harga ini tetapi ada indikasi hasil bisa berubah
    REVERSAL_ALERTS: bool = True
    REVERSAL_MIN_PRICE: float = 0.90
    REVERSAL_MOMENTUM: float = 0.08            # bracket sebelah naik ≥ 8¢ dalam 30 menit
    # Hanya market volume besar & likuid: volume event hari itu ≥ ini ($), favorit spread ≤ ini dan
    # kedalaman bid (≤3¢ dari bid terbaik) ≥ ini ($) — supaya posisi benar-benar bisa dijual.
    REVERSAL_MIN_VOLUME: float = 20000.0
    REVERSAL_MAX_SPREAD: float = 0.03
    REVERSAL_MIN_DEPTH_USD: float = 100.0
    # "Benar berbalik": harga favorit jatuh di bawah ini dan bracket lain memimpin
    REVERSAL_CONFIRM_PRICE: float = 0.50

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
    # Anti-spam: satu alert per wallet + market + sisi per hari, dan maksimal satu pesan per wallet tiap N menit
    # (transaksi di market yang juga ada di porto sendiri tetap dikirim tanpa menunggu jeda)
    WALLET_ALERT_COOLDOWN_MINUTES: int = 60
    # Alert saat porto sendiri (POLYMARKET_WALLET_ADDRESS) dan wallet yang diikuti memegang market yang sama
    WALLET_OVERLAP_ALERTS: bool = True
    WALLET_OVERLAP_CHECK_MINUTES: int = 10
    # Insider wallet: taruhan besar berpola "tahu lebih dulu" (lihat app/paper_trading/insider.py)
    INSIDER_ENABLED: bool = True
    INSIDER_ALERTS: bool = True
    INSIDER_SCAN_MINUTES: int = 5
    INSIDER_MIN_TRADE_USD: float = 1000.0      # trade yang diambil dari feed /trades
    INSIDER_MIN_BET_USD: float = 2000.0        # total per wallet + market + outcome
    INSIDER_MAX_PRICE: float = 0.50            # hanya beli sisi yang dinilai pasar ≤ 50%
    INSIDER_MIN_SCORE: int = 7                 # dari 15
    INSIDER_EXCLUDE_SPORTS: bool = True        # lewati olahraga/esports & "Up or Down"

    # Portfolio Polymarket sendiri (READ-ONLY). Alamat wallet = alamat di profil Polymarket.
    POLYMARKET_WALLET_ADDRESS: Optional[str] = None
    # Opsional, untuk saldo cash & open order (GET saja). Jangan pernah isi private key.
    POLYMARKET_API_KEY: Optional[str] = None
    POLYMARKET_API_SECRET: Optional[str] = None
    POLYMARKET_API_PASSPHRASE: Optional[str] = None
    POLYMARKET_SIGNER_ADDRESS: Optional[str] = None  # alamat pembuat API key (default = wallet address)
    POLYMARKET_SIGNATURE_TYPE: int = 1               # 1 = akun email/Magic, 2 = browser wallet, 0 = EOA

    # Auto paper trader (PAPER saja — tidak pernah mengirim order ke Polymarket)
    AUTOTRADE_ENABLED: bool = False            # status awal; bisa diubah lewat /startbot /stopbot
    # btc15 & maker_btc15 nonaktif: backtest 14 hari (1.344 market) negatif di semua aturan; sinyalnya tetap
    # dicatat (shadow) untuk riset.
    AUTOTRADE_STRATEGIES: str = "weather,weather_post,btc,maker_btc,hk_max,hk_min,hk_max_no,hk_min_no"
    AUTOTRADE_ORDER_USD: Decimal = Decimal("5.00")
    AUTOTRADE_MAX_DAILY_USD: Decimal = Decimal("50.00")   # total pembelian per hari (WIB)
    AUTOTRADE_MAX_DAILY_LOSS: Decimal = Decimal("20.00")  # stop hari itu jika rugi terealisasi ≥ ini
    AUTOTRADE_MAX_OPEN_USD: Decimal = Decimal("100.00")   # total posisi terbuka akun
    AUTOTRADE_MAX_PRICE: float = 0.90          # jangan beli di atas harga ini (ruang untung habis)
    AUTOTRADE_MAX_SPREAD: float = 0.05
    AUTOTRADE_FEE_RATE: float = 0.07           # fee taker = rate · p · (1 − p) per share, market ber-fee
    AUTOTRADE_BTC_MIN_EDGE: float = 0.05
    # Jangan beli sisi BTC di bawah harga ini: underdog murah = model berbeda pendapat dengan pasar, dan
    # di data live & backtest pasar yang lebih sering benar (live < 30¢: 3 menang dari 42).
    AUTOTRADE_BTC_MIN_PRICE: float = 0.30
    AUTOTRADE_BTC_WINDOW: str = "30-57"        # menit ke- dalam jam saat bot boleh masuk
    AUTOTRADE_BTC15_WINDOW: str = "7-14"       # menit ke- dalam rentang 15 menit
    AUTOTRADE_BTC5_WINDOW: str = "2-4"         # menit ke- dalam rentang 5 menit
    # Maker: limit BUY di (P_model − margin), tanpa fee; terisi bila ask menembus di bawah limit
    AUTOTRADE_MAKER_WINDOW: str = "15-50"
    AUTOTRADE_MAKER15_WINDOW: str = "3-12"
    AUTOTRADE_MAKER5_WINDOW: str = "1-4"
    # Simulasi slippage eksekusi nyata (¢ per share, 0.01 = 1¢): taker membayar ask VWAP + slippage;
    # limit maker baru dianggap terisi bila ask turun ≥ slippage di bawah harga limit.
    AUTOTRADE_SLIPPAGE: float = 0.01
    # Bobot model crypto vs harga pasar: peluang = pasar + bobot × (model − pasar). Kalibrasi 7 Okt 2026 (7.685
    # sinyal, out-of-sample): model penuh (1.0) kalah akurat dari pasar; 0.25–0.3 terbaik.
    AUTOTRADE_MODEL_WEIGHT: float = 0.3
    AUTOTRADE_MAKER_MARGIN: float = 0.04
    AUTOTRADE_MAKER_MIN_EDGE: float = 0.04
    AUTOTRADE_POLL_SECONDS: int = 10
    AUTOTRADE_WEATHER_MIN_EDGE: float = 0.05
    AUTOTRADE_WEATHER_REQUIRE_AGREEMENT: bool = True  # bracket perkiraan observasi = favorit pasar
    AUTOTRADE_WEATHER_SIGMA_C: float = 0.6     # ketidakpastian dasar perkiraan suhu (°C) + 0.3/jam ke puncak
    AUTOTRADE_WEATHER_POST_HOURS: float = 3.0  # weather_post: dari awal jam puncak s/d akhir puncak + N jam
    AUTOTRADE_REPORT_HOUR: int = 21            # jam WIB laporan harian
    # Bot Hong Kong (hk_max / hk_min): peluang per bracket dari max/min terukur HKO + proyeksi per jam
    # (Open-Meteo + bias HKO) + error historis proyeksi. Lihat docs/HK_BOT.md.
    AUTOTRADE_HK_MIN_EDGE: float = 0.08
    AUTOTRADE_HK_MODEL_WEIGHT: float = 0.6     # peluang = pasar + bobot × (model − pasar); dikalibrasi ulang dari data
    AUTOTRADE_HK_START_HOUR: float = 9         # jam HKT paling awal bot HK boleh membeli
    # Harga min 15¢: 10 Okt bot membeli bracket 6.7¢ jam 09:04 (model 23% vs pasar 5%) dan kalah — tiket murah jauh
    # sebelum puncak = model berbeda pendapat dengan pasar saat ketidakpastian terbesar.
    AUTOTRADE_HK_MIN_PRICE: float = 0.15
    AUTOTRADE_HK_LEAD_HOURS: float = 2.0       # masuk hanya ≤ 2 jam sebelum puncak/titik terendah (atau sesudahnya)
    # Sisi NO (hk_max_no / hk_min_no): beli NO bracket yang (hampir) mustahil. Untung kecil per trade, rugi penuh bila
    # salah — maka hanya bila peluang model YES ≤ 3% (atau sudah mustahil oleh angka terukur) dan edge ≥ 3¢.
    AUTOTRADE_HK_NO_MAX_PROB: float = 0.03
    AUTOTRADE_HK_NO_MIN_EDGE: float = 0.03
    AUTOTRADE_HK_NO_MAX_PRICE: float = 0.97
    AUTOTRADE_HK_OFFICIAL_WEIGHT: float = 0.5  # bobot angka prakiraan resmi HKO (bila disebut) dalam rata-rata
    AUTOTRADE_HK_AUTO_CALIBRATE: bool = True   # kalibrasi harian bias & bobot model (hk_calibration.py)
    # Hujan diperkirakan ±2 jam (nowcast radar HKO / peringatan hujan & petir / sedang hujan): sisa kenaikan max
    # tinggal fraksi ini, min diturunkan sekian °C (hujan bisa menjatuhkan suhu 3–5°C). Disetel ulang oleh kalibrasi rezim.
    AUTOTRADE_HK_RAIN_RISE_KEEP: float = 0.4
    AUTOTRADE_HK_RAIN_MIN_DROP: float = 1.0
    # --- AI Hong Kong (Gemini): pandangan bayangan + ringkasan terjadwal + /tanya. Tidak menentukan pembelian. ---
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-3.8-flash"
    HK_AI_ENABLED: bool = True
    HK_AI_REPORT_HOURS: str = "0,3,6,9,12,15,18,21"  # jam laporan ringkasan otomatis
    HK_AI_REPORT_TZ: str = "Asia/Jakarta"            # zona jam laporan di atas
    HK_AI_ASK_PER_HOUR: int = 20                     # batas /tanya per jam (kendali biaya)
    TELEGRAM_AUTOTRADE_CHAT_ID: Optional[str] = None  # chat/grup terpisah untuk notif auto trade
    # --- Trading UANG ASLI (Polymarket CLOB). Mati secara default; lihat docs/LIVE_TRADING.md ---
    LIVE_TRADING: bool = False
    LIVE_STRATEGIES: str = "btc,eth"            # seri yang dieksekusi live (default: BTC & ETH 1 jam)
    LIVE_ORDER_USD: float = 1.0                 # nominal per order (minimal $1)
    LIVE_MAX_ORDER_USD: float = 25.0            # batas keras per order (LIVE_ORDER_USD tidak boleh melebihi)
    LIVE_MAX_DAILY_USD: float = 10.0            # total pembelian live per hari (WIB)
    LIVE_MAX_DAILY_LOSS: float = 5.0            # berhenti hari itu bila rugi live terealisasi ≥ ini
    LIVE_MAX_OPEN_USD: float = 5.0              # total posisi live yang belum resolve
    LIVE_MAX_SLIPPAGE: float = 0.02             # batas harga = ask saat sinyal + ini (dan edge tetap ≥ minimum)
    # Harga beli minimum KHUSUS live (terpisah dari BTC_MIN_PRICE paper). Data live 7 Okt: beli < 30¢ menang
    # 7 dari 42; ≥ 50¢ menang 8 dari 10.
    LIVE_MIN_PRICE: float = 0.30
    LIVE_DRY_RUN: bool = False                  # susun & catat order tanpa mengirim
    # Auto-claim kemenangan (redeem posisi yang sudah resolve) lewat Relayer Polymarket, tanpa gas.
    # Butuh kredensial Builder API (Polymarket → Settings → Builder). Akun EOA (tipe 0) tidak didukung.
    LIVE_AUTO_CLAIM: bool = True
    POLY_BUILDER_API_KEY: Optional[str] = None
    POLY_BUILDER_SECRET: Optional[str] = None
    POLY_BUILDER_PASSPHRASE: Optional[str] = None
    POLY_RELAYER_URL: str = "https://relayer-v2.polymarket.com"
    POLY_PRIVATE_KEY: Optional[str] = None      # private key wallet KHUSUS bot (bukan wallet utama)
    POLY_FUNDER_ADDRESS: Optional[str] = None   # alamat proxy/funder Polymarket (akun email/Magic & browser wallet)
    POLY_SIGNATURE_TYPE: int = 1                # 0 = EOA/MetaMask langsung, 1 = email/Magic, 2 = browser wallet proxy
    POLY_CLOB_HOST: str = "https://clob.polymarket.com"
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
