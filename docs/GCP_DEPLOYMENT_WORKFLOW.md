# 🚀 Panduan Deployment GCP VM & Skema Alur Pengembangan (Workflow)

Dokumen ini berisi panduan lengkap untuk:
1. **Deploy sistem Paper Trading ke Google Cloud Platform (GCP) Compute Engine VM**.
2. **Skema alur kerja (workflow) pengembangan**: bagaimana alur jika Anda menambah fitur atau memperbaiki bug di laptop lokal dan ingin merilisnya ke server GCP.

---

## 📐 Arsitektur Sistem di Server GCP

```text
┌─────────────────────────────────────────────────────────────┐
│                 Laptop Developer (Lokal)                   │
│   - Tulis kode / Fitur baru / Bugfix                       │
│   - Uji coba lokal (Unit Test & Local Dashboard)            │
│   - Git Commit & Push ke GitHub                            │
└──────────────────────────────┬──────────────────────────────┘
                               │ git push
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                    GitHub / GitLab Repo                     │
└──────────────────────────────┬──────────────────────────────┘
                               │ git pull
                               ▼
┌─────────────────────────────────────────────────────────────┐
│               GCP Compute Engine (Ubuntu VM)                │
│                                                             │
│   ┌─────────────────────────────────────────────────────┐   │
│   │ Nginx Reverse Proxy (Port 80/443 SSL Let's Encrypt) │   │
│   └──────────────────────────┬──────────────────────────┘   │
│                              │ forward :8000                │
│   ┌──────────────────────────▼──────────────────────────┐   │
│   │ Web Dashboard (FastAPI / Uvicorn)                   │   │
│   └──────────────────────────┬──────────────────────────┘   │
│                              │ query DB                     │
│   ┌──────────────────────────▼──────────────────────────┐   │
│   │ PostgreSQL Database (Docker Volume Persistent Data) │   │
│   └──────────────────────────▲──────────────────────────┘   │
│                              │ simpan transaksi             │
│   ┌──────────────────────────┴──────────────────────────┐   │
│   │ Paper Trading Engine Daemon (Background Worker)     │   │
│   │ (Menjalankan polling cuaca Polymarket 24/7)         │   │
│   └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## Bagian 1: Panduan Deployment ke GCP Compute Engine VM

### Langkah 1: Buat VM Instance di GCP Console
1. Buka [Google Cloud Console](https://console.cloud.google.com/) -> **Compute Engine** -> **VM instances**.
2. Klik **Create Instance**:
   - **Name**: `paper-trading-vm`
   - **Region**: Pilih yang terdekat (misal: `asia-southeast2` Jakarta atau `asia-southeast1` Singapura).
   - **Machine Configuration**: `E2 series` -> `e2-micro` (gratis pada free tier) atau `e2-small` (disarankan, 2GB RAM).
   - **Boot disk**: Ubuntu 22.04 LTS atau 24.04 LTS, Disk size 20 GB.
   - **Firewall**: Centang **Allow HTTP traffic** dan **Allow HTTPS traffic**.
3. Klik **Create**.

### Langkah 2: Atur Firewall VPC GCP (Buka Port Dashboard)
Secara default, GCP memblokir port selain port 22 (SSH), 80, dan 443. Jika Anda ingin membuka port 8000 secara langsung tanpa Nginx:
1. Masuk ke menu **VPC network** -> **Firewall**.
2. Klik **Create Firewall Rule**:
   - **Name**: `allow-paper-dashboard`
   - **Targets**: *All instances in the network*
   - **Source IPv4 ranges**: `0.0.0.0/0` (atau IP publik rumah/kantor Anda untuk keamanan ekstra).
   - **Protocols and ports**: Centang **TCP** dan isi `8000`.
3. Klik **Create**.

### Langkah 3: Koneksi ke VM via SSH
Dari halaman VM instances di browser, klik tombol **SSH**, atau gunakan terminal laptop Anda:
```bash
gcloud compute ssh paper-trading-vm --zone=YOUR_ZONE
```

### Langkah 4: Setup Lingkungan di VM (Docker, Git, Python)
Jalankan perintah berikut di dalam terminal VM:

```bash
# Update sistem
sudo apt update && sudo apt upgrade -y

# Install Git, curl, build-essential
sudo apt install -y git curl build-essential python3-pip python3-venv

# Install Docker & Docker Compose
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER
newgrp docker
```

### Langkah 5: Clone Repository & Konfigurasi .env di VM
```bash
# Clone repo Anda
git clone https://github.com/USERNAME/paper-trading.git
cd paper-trading

# Buat file konfigurasi produksi dari template
cp .env.example .env
nano .env
```
Sesuaikan isi `.env` di VM:
```env
# Database Configuration
DATABASE_URL=postgresql://postgres:postgres@postgres:5432/paper_trading
POSTGRES_USER=postgres
POSTGRES_PASSWORD=GantiPasswordKuat123!
POSTGRES_DB=paper_trading
POSTGRES_PORT=5433

# Risk & Simulation Settings
INITIAL_BALANCE=20.00
MAX_POSITION_SIZE=1.00
SLIPPAGE_BPS=30
SPREAD_BPS=20

# Telegram Bot Integration
TELEGRAM_BOT_TOKEN=token_bot_anda_disini
TELEGRAM_CHAT_ID=chat_id_anda_disini
```

---

### Langkah 6: Menjalankan Aplikasi di VM

Ada 2 cara yang bisa dipilih:

#### Pilihan A: Menggunakan Docker Compose (Paling Mudah & Rapi)
Gunakan `docker-compose.prod.yml` yang sudah disediakan di repository:

```bash
# Build dan jalankan background container (Postgres, Web Dashboard, & Telegram Bot Daemon)
docker compose -f docker-compose.prod.yml up -d --build

# Periksa status semua container (harus 3 running: postgres, dashboard, telegram_bot)
docker compose -f docker-compose.prod.yml ps

# Uji coba koneksi notifikasi Telegram dari container
docker compose -f docker-compose.prod.yml run --rm dashboard python -m app.paper test-telegram

# Pantau log bot Telegram secara langsung
docker compose -f docker-compose.prod.yml logs -f telegram_bot
```

Dashboard sekarang aktif di: `http://<IP-EKSTERNAL-GCP-VM>:8000`
Bot Telegram otomatis aktif dan merespon `/start`, `/status`, `/positions`, dll.

---

#### Pilihan B: Menggunakan Systemd Service (Sangat Ringan untuk RAM Kecil)
Jika menggunakan instance kecil (`e2-micro`), Anda bisa menjalankan database di Docker dan aplikasi Python via virtualenv + `systemd`.

1. **Jalankan PostgreSQL Database**:
   ```bash
   docker compose up -d
   ```

2. **Setup Virtualenv Python**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Buat Systemd Service untuk Web Dashboard**:
   ```bash
   sudo nano /etc/systemd/system/paper-dashboard.service
   ```
   Isi file:
   ```ini
   [Unit]
   Description=Paper Trading Web Dashboard
   After=network.target

   [Service]
   User=ubuntu
   WorkingDirectory=/home/ubuntu/paper-trading
   EnvironmentFile=/home/ubuntu/paper-trading/.env
   ExecStart=/home/ubuntu/paper-trading/.venv/bin/uvicorn app.dashboard:app --host 0.0.0.0 --port 8000
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```

4. **Buat Systemd Service untuk Trading Engine 24/7**:
   ```bash
   sudo nano /etc/systemd/system/paper-engine.service
   ```
   Isi file:
   ```ini
   [Unit]
   Description=Paper Trading Engine Bot
   After=network.target

   [Service]
   User=ubuntu
   WorkingDirectory=/home/ubuntu/paper-trading
   EnvironmentFile=/home/ubuntu/paper-trading/.env
   ExecStart=/home/ubuntu/paper-trading/.venv/bin/python -m app.paper start --strategy weather_v1
   Restart=always
   RestartSec=10

   [Install]
   WantedBy=multi-user.target
   ```

5. **Buat Systemd Service untuk Telegram Bot Interaktif 24/7**:
   ```bash
   sudo nano /etc/systemd/system/paper-bot.service
   ```
   Isi file:
   ```ini
   [Unit]
   Description=Paper Trading Telegram Bot Listener
   After=network.target
   
   [Service]
   User=ubuntu
   WorkingDirectory=/home/ubuntu/paper-trading
   EnvironmentFile=/home/ubuntu/paper-trading/.env
   ExecStart=/home/ubuntu/paper-trading/.venv/bin/python -m app.paper bot
   Restart=always
   RestartSec=10
   
   [Install]
   WantedBy=multi-user.target
   ```

6. **Aktifkan & Jalankan Service**:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now paper-dashboard
   sudo systemctl enable --now paper-engine
   sudo systemctl enable --now paper-bot

   # Memeriksa status service:
   sudo systemctl status paper-dashboard
   sudo systemctl status paper-engine
   sudo systemctl status paper-bot
   ```

---

## Bagian 2: Skema Alur Kerja (Workflow) Pengembangan Lokal ➡️ GCP VM

Saat Anda ingin **menambah fitur baru** (misalnya strategi cuaca baru, indikator baru) atau **memperbaiki bug**, ikuti skema standar berikut agar server produksi tetap stabil dan data trading tidak hilang.

```text
[Lokal: Buat Branch Baru]
           │
           ▼
[Lokal: Tulis Kode & Tambah Fitur/Fix]
           │
           ▼
[Lokal: Jalankan Unit Test (Harus Pass)]
           │
           ▼
[Lokal: Git Commit & Push ke GitHub]
           │
           ▼
[GCP VM: Pull Perubahan & Restart Service]
```

### 1. Tahap Pengembangan di Laptop (Lokal)

1. **Gunakan Git Branch Khusus**:
   Jangan coding langsung di branch `main`.
   ```bash
   # Buat branch baru untuk fitur atau perbaikan
   git checkout -b feature/tambah-indikator-angin
   # atau untuk bugfix:
   git checkout -b fix/perbaikan-slippage
   ```

2. **Lakukan Perubahan Kode**:
   - Tulis logika atau fitur baru Anda.
   - Jika ada dependensi Python baru, tambahkan ke `requirements.txt`:
     ```bash
     pip freeze > requirements.txt  # atau tulis langsung di requirements.txt
     ```

3. **Uji Coba Secara Lokal Sebelum Di-push**:
   Pastikan kode berjalan baik dan tidak merusak fungsi lain:
   ```bash
   # Jalankan semua unit tests
   python3 -m unittest discover tests -v

   # Uji dashboard secara lokal
   uvicorn app.dashboard:app --reload --port 8000
   ```

4. **Commit dan Gabungkan ke Branch `main`**:
   ```bash
   git add .
   git commit -m "feat: tambah indikator kecepatan angin untuk strategi weather_v2"
   git checkout main
   git merge feature/tambah-indikator-angin
   git push origin main
   ```

---

### 2. Tahap Rilis / Update ke Server GCP VM

Setelah kode terdorong ke GitHub, masuk ke server GCP dan deploy pembaruan:

#### Jika Menggunakan Metode Docker Compose:
```bash
# 1. Masuk ke folder proyek di VM
cd ~/paper-trading

# 2. Ambil update kode terbaru dari GitHub
git pull origin main

# 3. Rebuild dan restart container (zero downtime)
docker compose -f docker-compose.prod.yml up -d --build
```
> **Catatan Keamanan Data**: Data saldo virtual, trade history, dan open position Anda **TIDAK AKAN HILANG** saat rebuild, karena database PostgreSQL tersimpan di Docker Volume persistent (`postgres_data`).

#### Jika Menggunakan Metode Systemd:
```bash
# 1. Masuk ke folder proyek di VM
cd ~/paper-trading

# 2. Ambil update kode terbaru
git pull origin main

# 3. Update dependencies jika ada library baru di requirements.txt
source .venv/bin/activate
pip install -r requirements.txt

# 4. Restart service aplikasi
sudo systemctl restart paper-engine
sudo systemctl restart paper-dashboard

# 5. Cek log untuk memastikan berjalan normal
journalctl -u paper-engine -f -n 50
```

---

### 3. Opsi Tambahan: Otomatisasi CI/CD (GitHub Actions)

Jika Anda tidak ingin repot SSH ke server setiap kali ada update, Anda bisa menggunakan **GitHub Actions** untuk auto-deploy saat `git push origin main`.

Buat file `.github/workflows/deploy.yml` di repository:
```yaml
name: Deploy to GCP VM

on:
  push:
    branches: [ main ]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy via SSH
        uses: appleboy/ssh-action@master
        with:
          host: ${{ secrets.GCP_VM_IP }}
          username: ${{ secrets.GCP_VM_USER }}
          key: ${{ secrets.GCP_SSH_PRIVATE_KEY }}
          script: |
            cd ~/paper-trading
            git pull origin main
            docker compose -f docker-compose.prod.yml up -d --build
```

---

### 4. Panduan Troubleshooting: Jika Dashboard Tidak Membaca Market di GCP

Jika setelah deploy dashboard menampilkan pesan tidak ada data atau tidak bisa membaca market:

#### A. Penyebab Umum & Solusi Otomatis yang Sudah Diterapkan:
1. **Perbedaan Jaringan Docker (`localhost` vs `postgres`)**:
   - Di dalam Docker, `localhost` merujuk ke container itu sendiri, bukan ke database PostgreSQL.
   - **Solusi**: `docker-compose.prod.yml` kini secara eksplisit menginjeksi `DATABASE_URL=postgresql://...postgres:5432/...` dan `app/core/config.py` memiliki auto-resolver cerdas saat mendeteksi environment Docker.
2. **Inisialisasi Tabel Database Baru**:
   - Saat database PostgreSQL pertama kali dibuat di GCP, tabel `market_snapshots` belum ada.
   - **Solusi**: FastAPI Dashboard kini secara otomatis menjalankan `init_db()` (`Base.metadata.create_all`) saat server pertama kali menyala (*lifespan startup*).
3. **Konektivitas Gamma API pada IP Cloud VM**:
   - Beberapa IP datacenter GCP terkadang mengalami timeout atau pembatasan dari Cloudflare Polymarket.
   - **Solusi**: Sistem dilengkapi mekanisme **Auto-Bootstrap** (`ensure_initial_market_snapshots`). Jika Gamma API belum selesai mengambil data atau terhambat jaringan cloud, sistem langsung mengisi baseline pasar cuaca Polymarket lengkap (Temperature, Precipitation, Wind/Storm, Snow) sehingga dashboard langsung aktif seketika.

#### B. Perintah Diagnostik di Server GCP:
```bash
# 1. Pastikan semua container berstatus Up (healthy)
docker compose -f docker-compose.prod.yml ps

# 2. Periksa log startup dashboard
docker compose -f docker-compose.prod.yml logs -f --tail=50 dashboard

# 3. Periksa log collector
docker compose -f docker-compose.prod.yml logs -f --tail=50 collector

# 4. Cek langsung data pasar di PostgreSQL
docker exec -it paper_trading_postgres psql -U postgres -d paper_trading -c "SELECT count(*), category FROM market_snapshots GROUP BY category;"

# 5. Jalankan satu siklus pengumpulan data manual jika diperlukan
docker exec -it paper_trading_dashboard python -m app.market_collector.run --once
```

---

## 🛡️ 4 Aturan Penting (Best Practices)

1. **Jangan Pernah Mengunggah File `.env` ke Git**:
   File `.env` berisi credential dan token Telegram. Pastikan `.gitignore` selalu aktif.
2. **Pisahkan Token Telegram**:
   Gunakan bot Telegram atau group ID yang berbeda untuk testing lokal vs produksi cloud agar notifikasinya tidak tercampur.
3. **Backup Database Rutin**:
   Untuk mencadangkan data transaksi paper trading di VM:
   ```bash
   docker exec -t paper_trading_postgres pg_dump -U postgres paper_trading > backup_$(date +%Y%m%d).sql
   ```
4. **Gunakan Nginx + SSL jika Dashboard Diakses Publik**:
   Jika dashboard akan dibuka dari mana saja, pasang Nginx dan sertifikat SSL gratis via Let's Encrypt (`certbot --nginx`) agar data login dan visual dashboard terenkripsi (HTTPS).
