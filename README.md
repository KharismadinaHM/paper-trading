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
- **📊 Metrik Kinerja Portofolio**:
  - **Maximum Drawdown**: Menggunakan pelacakan *High-Water Mark* (HWM) dinamis dari setiap *local peak* ke *trough* berikutnya.
  - **Win Rate & ROI**: Perhitungan closed trades murni dan return on investment.
  - **Mark-to-Market (MTM)**: Valuasi posisi terbuka berdasarkan harga pasar terkini.
- **🗄️ Skema Database PostgreSQL (SQLAlchemy 2.0)**:
  - Definisi tabel lengkap: `paper_accounts`, `paper_orders`, `paper_positions`, `paper_trades`, dan `paper_balance_snapshots`.
  - Dilengkapi *Check Constraints* untuk integritas data finansial ($\ge 0$).
- **💻 CLI Terminal Modern (Typer + Rich)**:
  - Antarmuka command-line informatif dengan tabel dan pewarnaan data finansial.
- **🌐 Web Dashboard (FastAPI + Jinja2 + Chart.js)**:
  - Tampilan visual responsif (Bootstrap) untuk ringkasan akun, tabel posisi terbuka, riwayat trade, dan grafik kurva ekuitas dinamis.
- **📱 Notifikasi Telegram**:
  - Pemformatan pesan otomatis untuk event *Paper BUY* dan *Paper Trade Settled*.

---

## 📁 Struktur Direktori

```text
.
├── app/
│   ├── core/
│   │   ├── config.py           # Konfigurasi aplikasi via pydantic-settings
│   │   └── logging.py          # Konfigurasi logging terpusat (console & file rotator)
│   ├── paper_trading/
│   │   ├── models.py           # Model ORM SQLAlchemy & skema relasional
│   │   ├── settlement_engine.py# Engine kalkulasi saham, slippage, & settlement
│   │   ├── metrics.py          # Kalkulasi metrik performa & drawdown
│   │   └── telegram.py         # Format & pengiriman notifikasi Telegram
│   ├── dashboard.py            # Aplikasi web FastAPI & rendering template
│   ├── paper.py                # Command Line Interface (CLI) utama
│   ├── paper_service.py        # Service layer & data interface
│   └── templates/
│       └── dashboard.html      # Template HTML dashboard (Jinja2 + Bootstrap)
├── tests/
│   ├── test_config_logging.py  # Unit test konfigurasi & logging
│   ├── test_metrics.py         # Unit test kalkulasi metrik & drawdown
│   ├── test_settlement_engine.py # Unit test kalkulasi settlement & proteksi risiko
│   └── test_telegram.py        # Unit test format pesan Telegram
├── docker-compose.yml          # Setup PostgreSQL container
├── requirements.txt            # Dependensi Python
├── .env.example                # Template variabel lingkungan
├── Paper Trading Feature.md    # Dokumen spesifikasi fungsional sistem
├── AUDIT_REPORT.md             # Dokumen laporan komprehensif audit QA
└── README.md                   # Dokumentasi utama proyek
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
```

### 3. Konfigurasi Lingkungan (`.env`)
Salin file template `.env.example` menjadi `.env`:

```bash
cp .env.example .env
```

Atur parameter di dalam file `.env`:
```ini
# Trading Rules
INITIAL_BALANCE=20.00
MAX_POSITION_SIZE=1.00
SLIPPAGE_BPS=30
SPREAD_BPS=20
FEE_RATE_BPS=0

# Database Connection
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/paper_trading

# Telegram Notifications (Opsional)
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
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

> 💡 **Petunjuk Menghubungkan Bot Telegram**: Lihat panduan lengkap di [docs/TELEGRAM_SETUP.md](file:///Users/kharismadinahijram/paper-trading/docs/TELEGRAM_SETUP.md).

### 2. Menjalankan Web Dashboard
Jalankan server pengembangan FastAPI dengan Uvicorn:

```bash
uvicorn app.dashboard:app --reload --port 8000
```
Buka peramban di: **`http://localhost:8000`**

Fitur dashboard:
- **Account Summary Card**: Saldo berjalan, Realized P/L, dan ROI.
- **Performance Overview**: Total trade, Win Rate, dan Maximum Drawdown.
- **Equity Curve**: Grafik interaktif saldo vs. waktu menggunakan Chart.js.
- **Tabel Data**: Posisi aktif dan riwayat transaksi tertutup.
- **Filter Strategi**: Opsi filter parameter URL (contoh: `http://localhost:8000/?strategy=weather_v1`).

---

## 🧪 Menjalankan Pengujian (Testing)

Proyek ini dilengkapi dengan rangkaian pengujian unit menyeluruh (`unittest`):

```bash
# Menjalankan seluruh test suite
python3 -m unittest discover tests -v

# Menjalankan unit test settlement engine secara spesifik
python3 -m unittest tests/test_settlement_engine.py -v

# Menjalankan unit test metrik & drawdown
python3 -m unittest tests/test_metrics.py -v
```

Semua 30/30 unit test saat ini berada dalam status **PASS**.

---

## ☁️ Deployment ke GCP VM & Alur Pengembangan

Untuk petunjuk lengkap deployment ke cloud dan alur kerja pembaruan fitur/bugfix:
- 📖 **[Panduan Lengkap Deployment GCP & Skema Workflow](docs/GCP_DEPLOYMENT_WORKFLOW.md)**:
  - Setup instance Compute Engine (e2-micro / e2-small) & Firewall rule.
  - Opsi deployment via Docker Compose (`docker-compose.prod.yml`) atau `systemd` daemon (24/7).
  - Skema update dari laptop lokal ke server GCP (Git workflow, CI/CD auto-deploy, zero data loss pada database).

---

## 📚 Dokumen Referensi Terkait

- 📄 **[Paper Trading Feature.md](Paper%20Trading%20Feature.md)**: Dokumen desain asli dan spesifikasi fungsional fitur Paper Trading.
- 📋 **[AUDIT_REPORT.md](AUDIT_REPORT.md)**: Laporan komprehensif audit QA (trace numerik, temuan bug, dan status arsitektur).
- 🚀 **[GCP_DEPLOYMENT_WORKFLOW.md](docs/GCP_DEPLOYMENT_WORKFLOW.md)**: Panduan rilis cloud GCP & siklus development.

---

## ⚖️ Lisensi & Catatan Keamanan
Proyek ini dibuat untuk tujuan riset dan simulasi algoritma perdagangan. Tidak ada jaminan keuntungan finansial di pasar prediksi Polymarket sebenarnya.
