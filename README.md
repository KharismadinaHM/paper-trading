# 📈 Polymarket Weather Paper Trading System

Sistem simulasi perdagangan (*paper trading*) real-time untuk pasar prediksi cuaca di Polymarket. Dirancang sebagai jembatan validasi antara fase *backtest* dan penggunaan modal nyata (*real-money trading*), memungkinkan evaluasi performa strategi dengan data pasar aktual tanpa risiko finansial.

---

## 🌟 Fitur Utama

- **🔢 Pure Decimal Calculation Engine**:
  - Semua kalkulasi finansial (`shares`, `pnl`, `fees`, `roi`) menggunakan modul `Decimal` Python untuk menghindari floating-point imprecision.
  - Rounding standar 4 desimal konsisten (`ROUND_HALF_UP`).
- **🛡️ Anti-Cheating & Risk Management**:
  - Penolakan order tanpa data harga historis (mencegah *Lookahead Bias*).
  - Validasi saldo mencukupi dan pembatasan ukuran posisi maksimum (`MAX_POSITION_SIZE`).
  - Pemodelan slippage dan spread pasar yang realistis (`apply_slippage_and_spread`).
  - Validasi atomik: order yang ditolak oleh risk engine langsung dihentikan tanpa *partial write* ke database.
- **🛰️ Polymarket Weather Market Collector**:
  - Pengumpulan data otomatis dari publik Gamma API Polymarket (`https://gamma-api.polymarket.com`).
  - Pencarian pasar cuaca (suhu, curah hujan, salju, badai/angin) via search queries dan general markets.
  - **Pencocokan eksplisit outcome Yes/No** (tidak pernah mengasumsikan index 0 selalu 'Yes').
  - Pencatatan time-series snapshot ke PostgreSQL tanpa pernah menimpa (*overwrite*) data histori lama.
- **⏱️ Anti-Stale Price Protection & Freshness Check**:
  - Backend selalu me-fetch ulang harga real-time live saat user mengeksekusi order (mengabaikan harga payload frontend untuk settlement).
  - Peringatan divergensi harga jika harga pasar bergerak $> 5\%$ dari saat user melihat tombol buy.
  - Deteksi snapshot kadaluarsa (`is_stale: bool`) jika snapshot lebih tua dari threshold 15 menit (3x siklus collector).
- **🔍 Suggested Markets & Live Search**:
  - **Suggested Markets**: Filter pasar aktif dengan waktu resolusi mendekati sekarang ($\le 6$ jam) dan harga optimal (0.70 - 0.75).
  - **Live Search**: Pencarian multi-kriteria berdasarkan kata kunci, kategori, dan rentang harga.
  - Terintegrasi langsung dengan modal "Paper Buy" di antarmuka Web Dashboard.
- **📊 Metrik Kinerja Portofolio**:
  - **Maximum Drawdown**: Menggunakan pelacakan *High-Water Mark* (HWM) dinamis dari setiap *local peak* ke *trough* berikutnya.
  - **Win Rate & ROI**: Perhitungan closed trades murni dan return on investment.
  - **Mark-to-Market (MTM)**: Valuasi posisi terbuka berdasarkan harga pasar terkini.
- **🗄️ Skema Database PostgreSQL (SQLAlchemy 2.0)**:
  - Definisi tabel lengkap: `paper_accounts`, `paper_orders`, `paper_positions`, `paper_trades`, `paper_balance_snapshots`, dan `market_snapshots`.
  - Dilengkapi *Check Constraints* dan *Index* untuk integritas data finansial ($\ge 0$).
- **💻 CLI Terminal Modern (Typer + Rich)**:
  - Antarmuka command-line informatif dengan tabel dan pewarnaan data finansial.
- **🌐 Web Dashboard (FastAPI + Jinja2 + Bootstrap + Chart.js)**:
  - Tampilan visual responsif untuk ringkasan akun, tabel posisi terbuka, riwayat trade, grafik kurva ekuitas, serta section Suggested & Search Markets.
- **📱 Notifikasi & Bot Interaktif Telegram**:
  - Pemformatan pesan otomatis untuk event *Paper BUY* dan *Paper Trade Settled*.
  - Bot interaktif dengan command: `/start`, `/status`, `/positions`, `/trades`, `/performance`, `/ping`.

---

## 📁 Struktur Direktori

```text
.
├── app/
│   ├── core/
│   │   ├── config.py             # Konfigurasi aplikasi via pydantic-settings
│   │   ├── database.py           # Inisialisasi engine & session maker SQLAlchemy
│   │   └── logging.py            # Konfigurasi logging terpusat (console & file rotator)
│   ├── market_collector/
│   │   ├── collector.py          # Logika fetch Gamma API & save timeseries snapshots
│   │   └── run.py                # Daemon runner loop & scheduler collector
│   ├── paper_trading/
│   │   ├── models.py             # Model ORM SQLAlchemy & skema relasional
│   │   ├── settlement_engine.py  # Engine kalkulasi saham, slippage, & settlement
│   │   ├── metrics.py            # Kalkulasi metrik performa & drawdown
│   │   ├── suggestions.py        # Filter suggested markets & pencarian pasar
│   │   └── telegram.py           # Format & pengiriman notifikasi Telegram
│   ├── dashboard.py              # Aplikasi web FastAPI & rendering template
│   ├── paper.py                  # Command Line Interface (CLI) utama
│   ├── paper_service.py          # Service layer terhubung ke DB PostgreSQL
│   └── templates/
│       └── dashboard.html        # Template HTML dashboard (Jinja2 + Bootstrap)
├── tests/
│   ├── test_config_logging.py    # Unit test konfigurasi & logging
│   ├── test_market_collector.py  # Unit test Market Collector & timeseries persistence
│   ├── test_market_search.py     # Unit test endpoint & logika search pasar
│   ├── test_market_suggestions.py# Unit test endpoint & logika filter saran pasar
│   ├── test_metrics.py           # Unit test kalkulasi metrik & drawdown
│   ├── test_orders_endpoint.py   # Unit test POST /api/orders & anti-stale protection
│   ├── test_paper_service_db.py  # Unit test query DB real get_market_snapshots & by_id
│   ├── test_positions_and_sell.py# Unit test Polymarket positions, Paper Sell, deposit & filter
│   ├── test_settlement_engine.py # Unit test kalkulasi settlement & proteksi risiko
│   ├── test_telegram.py          # Unit test format pesan Telegram
│   └── test_telegram_bot.py      # Unit test command bot interaktif Telegram
├── docker-compose.yml            # Setup lokal PostgreSQL
├── docker-compose.prod.yml       # Setup multi-service production (PostgreSQL, Dashboard, Bot, Collector)
├── requirements.txt              # Dependensi Python
├── .env.example                  # Template variabel lingkungan
├── docs/
│   ├── GCP_DEPLOYMENT_WORKFLOW.md# Panduan rilis cloud GCP & siklus development
│   └── TELEGRAM_SETUP.md         # Panduan konfigurasi Telegram Bot
├── AUDIT_REPORT.md               # Dokumen laporan komprehensif audit QA
└── README.md                     # Dokumentasi utama proyek
```

---

## 🚀 Panduan Memulai

### 1. Prasyarat
- Python 3.10 atau versi yang lebih baru
- Docker & Docker Compose (untuk database PostgreSQL)

### 2. Instalasi Dependensi
Buat virtual environment dan pasang pustaka yang diperlukan:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest
```

### 3. Konfigurasi Lingkungan (`.env`)
Salin file template `.env.example` menjadi `.env`:

```bash
cp .env.example .env
```

Atur parameter di dalam file `.env`:
```ini
# Database Connection
DATABASE_URL=postgresql://postgres:postgres@localhost:5433/paper_trading
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DB=paper_trading
POSTGRES_PORT=5433

# Paper Trading Risk & Slippage Configuration
INITIAL_BALANCE=20.00
MAX_POSITION_SIZE=1.00
SLIPPAGE_BPS=30
SPREAD_BPS=20
FEE_RATE_BPS=0

# Market Collector Configuration
COLLECTOR_INTERVAL_SECONDS=300
GAMMA_API_BASE_URL=https://gamma-api.polymarket.com

# Telegram Notifications & Bot (Opsional)
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# Logging Configuration
LOG_LEVEL=INFO
LOG_FILE=logs/app.log
```

### 4. Menjalankan Database PostgreSQL
Jalankan service database menggunakan Docker Compose:

```bash
docker compose up -d
```

---

## 🖥️ Penggunaan Sistem

### 1. Menggunakan CLI (Command Line Interface)
CLI dibangun menggunakan **Typer** dan **Rich**:

```bash
# Memulai Paper Trading Engine
python -m app.paper start --strategy weather_v1

# Memeriksa status ringkasan akun (Balance, P/L, Win Rate)
python -m app.paper status

# Menampilkan tabel posisi terbuka (Open Positions)
python -m app.paper positions

# Menampilkan riwayat transaksi yang telah selesai (Trade History)
python -m app.paper trades

# Menampilkan analisis metrik kinerja
python -m app.paper performance --strategy weather_v1

# Mereset akun paper trading ke kondisi awal
python -m app.paper reset

# Menguji koneksi notifikasi Telegram
python -m app.paper test-telegram

# Menjalankan listener interaktif Telegram Bot (/start, /status, dll)
python -m app.paper bot
```

> 💡 **Petunjuk Menghubungkan Bot Telegram**: Lihat panduan lengkap di [docs/TELEGRAM_SETUP.md](docs/TELEGRAM_SETUP.md).

---

### 2. Menjalankan Market Collector Daemon
Market Collector mengambil pasar prediksi cuaca dari Polymarket Gamma API dan menyimpannya secara berkala ke database:

```bash
# Menjalankan daemon di background (siklus setiap 5 menit / 300 detik)
python -m app.market_collector.run

# Menjalankan 1 siklus saja lalu keluar (cocok untuk cron job / testing)
python -m app.market_collector.run --once

# Menjalankan daemon dengan interval kustom (misal: 60 detik)
python -m app.market_collector.run --interval 60
```

---

### 3. Menjalankan Web Dashboard
Jalankan server FastAPI dengan Uvicorn:

```bash
uvicorn app.dashboard:app --reload --port 8000
```
Buka peramban di: **`http://localhost:8000`**

Fitur dashboard bertema **Polymarket Dark Mode**:
- **Portfolio & Available Card**: Saldo kas riil, nilai portofolio dinamis (*Mark-to-Market*), tombol sembunyikan saldo (eye toggle), tombol **Deposit** & **Withdraw / Reset**.
- **Profit/Loss Card**: Nilai P/L dinamis (Realized + Unrealized), filter rentang waktu (`1D`, `1W`, `1M`, `1Y`, `YTD`, `ALL`), dan grafik sparkline area glowing.
- **Tabel Posisi Dinamis (Polymarket Style)**:
  - **Tampilan Judul Penuh**: Judul pertanyaan pasar cuaca tampil lengkap dan tidak terpotong teksnya (*no premature truncation*).
  - **Direct Polymarket Reference Link**: Mengklik judul pasar akan langsung membuka link rujukan pasar terkait di platform **Polymarket** pada tab baru (`target="_blank"`).
  - Kolom: `Market` (ikon cuaca, tautan pertanyaan pasar ke Polymarket asli, badge outcome `No 70.3¢` / `Yes 60¢`, jumlah shares), `Avg → Now`, `Traded`, `To win` (potensi payout), `Value` (dengan persentase PnL & ROI berwarna), dan tombol **Sell**.
- **Suggested & Search Markets dengan Polymarket Link**: Kartu rekomendasi dan pencarian pasar cuaca juga dilengkapi tautan langsung ke halaman pasar Polymarket asli.
- **Paper Sell Feature**: Jual posisi terbuka secara parsial maupun penuh langsung pada harga pasar real-time live, mengkredit saldo kas, dan mencatat riwayat transaksi.
- **Tabs Navigasi**: Tab `Positions`, `Open Orders`, dan `History` dengan *instant search* dan dropdown pengurutan.
- **Suggested Markets**: Rekomendasi pasar probabilitas tinggi dengan tombol **Paper Buy**.
- **Search Market Lengkap**:
  - Filter Kategori: `Temperature` (Highest/Lowest Temp), `Precipitation` (Rain), `Wind / Storm`, `Snow`.
  - Filter Waktu Selesai (*Ending Soon*): `< 6 Hours`, `< 24 Hours`, `< 3 Days`, `< 7 Days`, `< 30 Days` disertai badge urgensi berwarna.
  - Sorting: *Ending Soonest*, *Ending Latest*, *Highest Price*, *Lowest Price*, *Market Name*.

---

### 4. Integrasi Telegram Bot & Notifikasi Real-time
Bot Telegram (`app/paper_trading/telegram_bot.py`) dan notifikasi sinyal (`telegram.py`) telah diperbarui:
- **`/positions`**: Menampilkan daftar posisi terbuka lengkap dengan metrik `Avg → Now`, `Traded`, `To Win`, `Value`, `Floating P/L`, dan tautan langsung `🌐 [Buka di Polymarket](https://polymarket.com/markets?_q=...)`.
- **`/status`**: Menampilkan ringkasan akun komprehensif mencakup *Portfolio Value (Mark-to-Market)*, *Saldo Kas (Cash)*, *Terinvestasi*, *Floating P/L*, *Realized P/L*, dan *Total P/L*.
- **`/trades`**: Menampilkan riwayat transaksi lengkap dengan tautan pasar Polymarket.
- **Notifikasi Signal**: Notifikasi otomatis saat Paper BUY dan Settlement kini menyertakan link rujukan pasar Polymarket.

---

### 5. REST API Endpoints

Sistem menyediakan API publik dan internal:

| Method | Endpoint | Deskripsi |
|---|---|---|
| `GET` | `/api/markets/suggestions` | Mengambil saran pasar mendekati resolusi ($\le 6$ jam, harga 0.70-0.75) dengan `polymarket_url` |
| `GET` | `/api/markets/search` | Pencarian pasar cuaca dengan filter kata kunci, kategori, harga, sisa waktu, dan `polymarket_url` |
| `POST` | `/api/orders` | Membuat paper order manual dengan proteksi Anti-Stale Price |
| `POST` | `/api/positions/sell` | Menjual/menutup posisi terbuka pada harga pasar live (*Paper Sell*) |
| `POST` | `/api/account/deposit` | Menambah saldo akun paper trading (*Paper Deposit*) |
| `POST` | `/api/account/reset` | Mereset akun ke saldo awal $20.00 dan me-refresh posisi default |
| `GET` | `/api/positions` | Mengambil daftar posisi terbuka dengan valuasi dynamic Mark-to-Market & `polymarket_url` |
| `GET` | `/api/trades` | Mengambil riwayat transaksi selesai |
| `GET` | `/api/summary` | Ringkasan saldo, portofolio, dan performa akun |

---

## 🧪 Menjalankan Pengujian (Testing)

Proyek ini dilengkapi dengan rangkaian pengujian unit menyeluruh (`pytest`):

```bash
# Menjalankan seluruh test suite (105 test)
pytest -v

# Menjalankan unit test modul spesifik
pytest tests/test_positions_and_sell.py -v
pytest tests/test_telegram_bot.py -v
pytest tests/test_market_collector.py -v
pytest tests/test_paper_service_db.py -v
pytest tests/test_orders_endpoint.py -v
pytest tests/test_settlement_engine.py -v
```

Saat ini seluruh **107/107 unit test** berada dalam status **PASS**.

---

## ☁️ Deployment ke GCP VM & Multi-Service Docker

Untuk deployment terpadu menggunakan Docker Compose multi-service di server produksi:

```bash
# Menjalankan seluruh stack: PostgreSQL, Web Dashboard, Telegram Bot, dan Market Collector
docker compose -f docker-compose.prod.yml up -d --build
```

Lihat petunjuk lengkap deployment cloud dan alur kerja pembaruan fitur di:
- 📖 **[Panduan Lengkap Deployment GCP & Skema Workflow](docs/GCP_DEPLOYMENT_WORKFLOW.md)**

---

## ⚖️ Lisensi & Catatan Keamanan
Proyek ini dibuat untuk tujuan riset dan simulasi algoritma perdagangan. Tidak ada jaminan keuntungan finansial di pasar prediksi Polymarket sebenarnya.
