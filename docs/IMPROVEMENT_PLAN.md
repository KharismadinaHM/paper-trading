# 🛠️ Improvement Plan — Polymarket Weather Paper Trading

Dokumen ini adalah rencana perbaikan berdasarkan hasil QA (review kode, 107 unit test, dan uji perilaku langsung ke service layer). Tujuannya membawa sistem dari kondisi "demo UI" menjadi **paper trading yang datanya bisa dipercaya** untuk memvalidasi strategi sebelum memakai modal nyata.

---

## ✅ Status Implementasi (update 2026-09-24)

| Fase | Selesai | Belum / ditunda |
|---|---|---|
| 0 | 0.3 fail-fast `APP_ENV=production` (PR #2), 0.1 HTTP Basic Auth (`DASHBOARD_PASSWORD`), 0.4 `escapeHtml`/`safeUrl`, 0.5 tolak market kedaluwarsa, 0.6 sell side harus cocok, 0.7 pin dependensi + `requirements-dev.txt`, 0.8 Docker non-root | 0.2 port 8000 masih terbuka (dilindungi auth; reverse proxy + HTTPS disarankan) |
| 1 | 1.1 Alembic baseline + `alembic check` di CI (PR #2), 1.2–1.10: seluruh state di DB, row lock saldo, akun default dari `INITIAL_BALANCE`, data demo & angka hardcoded dihapus, equity curve dari `paper_balance_snapshots`, metrik satu sumber, ledger deposit (`paper_cash_movements`) sebagai basis ROI, bot & CLI memakai DB yang sama | 1.11 pemisahan `action` vs `outcome` |
| 2 | 2.1–2.7: sinkronisasi market posisi terbuka (termasuk closed), tabel `market_resolutions`, settlement worker idempotent di loop collector, notifikasi Telegram buy & settled, INVALID → refund, status `CLOSED` untuk jual manual | — |
| 3 | 3.1 limit eksposur per market & total, 3.2 tolak harga stale (`REJECT_STALE_ORDERS`), 3.3 slippage sisi jual, 3.4 sell ditolak tanpa harga live, 3.6 minimum notional, 3.7 sumber harga kartu = harga eksekusi, 3.8 fetch market on-demand by `conditionId` | 3.5 API request/response masih `float` (kalkulasi internal sudah `Decimal`) |
| 4 | 4.3 retensi snapshot & 4.6 paginasi event bertag cuaca (PR #2), 4.1 index `(market_id, timestamp)`, 4.4 harga posisi dalam satu query, 4.5 market sintetis hanya jika `ALLOW_SYNTHETIC_MARKETS=true`, 4.7 `/healthz` + healthcheck compose | 4.2 tabel `market_latest`, 4.8 logging JSON |
| 5 | 5.5–5.6 CI GitHub Actions dengan PostgreSQL + build Docker (PR #2), 5.1 import fallback dihapus di file yang disentuh, 5.2 pool hanya untuk PostgreSQL, 5.7 README diperbarui | 5.3–5.4 refactor file besar |

Seluruh 15 item checklist Definition of Done di bagian 4 sudah memiliki test otomatis (150 test lulus setelah PR #2).
Konkurensi juga diverifikasi di PostgreSQL: 4 proses paralel × 6 order $1 dengan saldo $20 → tepat 20 order tereksekusi, saldo akhir $0.

---

## 1. Ringkasan Kondisi Saat Ini

| Area | Status | Catatan |
|---|---|---|
| Unit test | ✅ 107 passed | Sebagian besar menguji stub in-memory, belum menguji alur sebenarnya |
| Market Collector | 🟡 Berjalan | Hanya mencatat market aktif; market resolved tidak pernah tercatat |
| State akun / posisi / trade | 🔴 In-memory + data demo | Hilang saat restart, tidak konsisten antar proses |
| Settlement | 🔴 Tidak ada | `calculate_settlement` tidak pernah dipanggil |
| Risk control | 🟠 Parsial | Hanya limit per order |
| Keamanan | 🔴 Tanpa autentikasi | Endpoint reset/deposit terbuka publik |

### Bukti hasil uji perilaku

| Skenario | Hasil |
|---|---|
| Akun baru, modal $20 | Portfolio value **$24.50** (posisi seed tidak mengurangi kas) |
| `realized_pnl` status vs performance | **2.87** vs **0.43** (tidak konsisten) |
| Order di market yang `end_date`-nya lewat 3 jam | **Diterima** |
| Sell side `YES` pada posisi yang hanya `NO` | **Posisi NO terjual** diam-diam |
| 6 × order $1 di market yang sama (`MAX_POSITION_SIZE=1`) | Posisi **$6** |
| Sell 0.0001 share | Proceeds $0, status **WON** |
| Deposit $100 | ROI tetap dihitung dari modal $20 |

---

## 2. Prinsip Perbaikan

1. **Database adalah satu-satunya sumber data (single source of truth).** Dashboard, CLI, dan bot Telegram membaca dari tempat yang sama.
2. **Tidak ada data sintetis di jalur produksi.** Data demo hanya boleh ada di `APP_ENV=development` dan harus ditandai.
3. **Tolak, jangan sekadar memperingatkan**, jika kondisi eksekusi tidak realistis (market kedaluwarsa, harga stale, harga tidak tersedia).
4. **Setiap perbaikan disertai test regresi** yang mereproduksi bug di atas.

---

## 3. Roadmap

```
Fase 0  Quick wins & keamanan          ~1–2 hari
Fase 1  Fondasi database (state nyata) ~3–5 hari   ← paling kritis
Fase 2  Settlement & lifecycle market  ~3–4 hari
Fase 3  Risk engine & akurasi eksekusi ~2–3 hari
Fase 4  Performa & operasional         ~2–3 hari
Fase 5  Kualitas kode & test           berjalan paralel
```

Fase 1 adalah prasyarat Fase 2 dan 3. Fase 0 bisa dikerjakan segera dan independen.

---

## Fase 0 — Quick Wins & Keamanan

| ID | Tugas | File | Kriteria selesai |
|---|---|---|---|
| 0.1 | Tambah autentikasi (API token via header atau HTTP Basic) untuk semua endpoint `POST` dan dashboard | `app/dashboard.py`, `app/core/config.py` | Request tanpa token → `401` |
| 0.2 | Jangan ekspos port 8000 langsung; bind ke `127.0.0.1` dan taruh di belakang reverse proxy (Caddy/Nginx + HTTPS) | `docker-compose.prod.yml` | Port 8000 tidak dapat diakses dari luar VM |
| 0.3 | Wajibkan `POSTGRES_PASSWORD` non-default di produksi (fail-fast jika `postgres`) | `app/core/config.py` | App menolak start di `APP_ENV=production` dengan password default |
| 0.4 | Buat helper `escapeHtml()` di JS dan gunakan untuk semua data dari API (`group_item_title`, `image`, `condition_id`, `market_name`, dll.) | `app/templates/dashboard.html` | Judul market berisi `"` atau `<script>` tampil sebagai teks |
| 0.5 | Tolak order jika `now >= end_date/resolution_time` | `app/paper_service.py` (`create_paper_order`) | Order di market kedaluwarsa → `400` |
| 0.6 | Sell harus mencocokkan side secara persis; hapus fallback "cari key lain" | `app/paper_service.py:409-416` | Sell side salah → `404` |
| 0.7 | Pin versi dependensi (`requirements.txt` dengan `==` atau lock file) dan samakan versi Python lokal dengan Docker (3.12) | `requirements.txt`, `Dockerfile` | Build reproducible |
| 0.8 | Jalankan container sebagai non-root user | `Dockerfile` | `USER app` di Dockerfile |

---

## Fase 1 — Fondasi Database (State Nyata)

**Masalah:** `_account_state`, `_paper_positions`, `_paper_orders`, `_trade_history` adalah dict global di memori ([paper_service.py:51-169](../app/paper_service.py)). Data hilang saat restart, dan container dashboard vs bot Telegram memiliki state berbeda.

| ID | Tugas | Kriteria selesai |
|---|---|---|
| 1.1 | Pasang **Alembic**; buat migrasi awal dari model yang ada | `alembic upgrade head` membuat semua tabel; `create_all` hanya untuk test |
| 1.2 | Buat repository/service berbasis `Session` untuk `PaperAccount`, `PaperPosition`, `PaperOrder`, `PaperTrade`, `PaperBalanceSnapshot` | Tidak ada lagi dict global di `paper_service.py` |
| 1.3 | Bootstrap akun default dari `settings.INITIAL_BALANCE` jika belum ada (idempotent) | Akun baru: kas = modal awal, posisi = 0, P/L = 0 |
| 1.4 | `create_paper_order` dalam satu transaksi: lock akun (`SELECT … FOR UPDATE`) → validasi → insert order + upsert posisi + kurangi saldo → insert balance snapshot | Order bersamaan tidak membuat saldo negatif / tidak konsisten |
| 1.5 | `sell_paper_position` dalam transaksi yang sama polanya; tulis `PaperTrade` dengan `exit_price`, `net_pnl` | Trade tersimpan permanen |
| 1.6 | Hapus semua data seed & angka hardcoded: posisi/trade demo, `win_rate=0.75`, `realized_pnl=2.87`, `+Decimal("0.11")`, equity curve statis | `grep` tidak menemukan angka-angka tersebut di `app/` |
| 1.7 | Equity curve dibangun dari `paper_balance_snapshots` | Grafik mencerminkan histori nyata |
| 1.8 | Win rate, realized P/L, ROI dihitung dari `paper_trades` via `calculate_performance_metrics` (satu fungsi untuk status & performance) | Angka status == angka performance |
| 1.9 | Catat deposit sebagai ledger (tabel `paper_deposits` atau kolom `total_deposits`); ROI = total P/L ÷ total modal disetor | Deposit $100 mengubah basis ROI |
| 1.10 | Bot Telegram & CLI memakai service DB yang sama | `/status` di Telegram == dashboard |
| 1.11 | Hapus `TradeSide.BUY/SELL` untuk posisi; gunakan `YES/NO` untuk outcome dan kolom terpisah `action` (`BUY`/`SELL`) di order | Tidak ada lagi normalisasi `BUY→YES` di banyak tempat |

**Test regresi wajib:** restart persistence, konsistensi status vs performance, 20 order paralel tidak merusak saldo, akun baru bernilai tepat `INITIAL_BALANCE`.

---

## Fase 2 — Settlement & Lifecycle Market

**Masalah:** Posisi tidak pernah di-settle. Collector memakai `active_only=True`, jadi market yang sudah tutup tidak pernah mendapat snapshot `resolved`, sehingga snapshot terakhirnya "open" selamanya.

| ID | Tugas | File | Kriteria selesai |
|---|---|---|---|
| 2.1 | Collector: untuk setiap `market_id` yang punya posisi terbuka, fetch status market per `conditionId` (termasuk closed) dan simpan snapshot dengan outcome pemenang | `app/market_collector/collector.py` | Market yang tutup tercatat `is_resolved=True` + `winning_outcome` |
| 2.2 | Tambah kolom `winning_outcome` (`YES`/`NO`/`INVALID`) di `market_snapshots` (migrasi Alembic) | `models.py` | — |
| 2.3 | Buat **settlement worker** (bisa di dalam loop collector): posisi terbuka di market resolved → `calculate_settlement` → tulis `PaperTrade` (WON/LOST) → tambah payout ke saldo → hapus posisi → balance snapshot | `app/paper_trading/settlement_engine.py`, modul baru `settlement_worker.py` | Posisi otomatis tertutup setelah market resolve |
| 2.4 | Settlement idempotent (tidak dobel jika worker berjalan ulang) | — | Unique constraint / flag `settled_at` |
| 2.5 | Panggil `notify_paper_buy` saat order dan `notify_paper_settled` saat settlement | `app/paper_trading/telegram.py` | Notifikasi terkirim |
| 2.6 | Tangani market `INVALID`/cancelled: kembalikan modal, status `CANCELLED` | — | Test khusus |
| 2.7 | Bedakan status trade: `WON`/`LOST` untuk settlement, `CLOSED` untuk manual sell (P/L tetap dicatat) | `models.py` | Win rate tidak tercampur dengan exit manual |

---

## Fase 3 — Risk Engine & Akurasi Eksekusi

| ID | Tugas | File | Kriteria selesai |
|---|---|---|---|
| 3.1 | Tambah `MAX_EXPOSURE_PER_MARKET` dan `MAX_TOTAL_EXPOSURE` ke `evaluate_risk_and_rules` | `settlement_engine.py`, `config.py` | 6 × $1 di market yang sama ditolak jika limit $1 |
| 3.2 | Tolak order jika snapshot stale (> `3 × COLLECTOR_INTERVAL_SECONDS`), bukan hanya warning | `paper_service.py` | Order di data stale → `409` |
| 3.3 | Sell memakai `apply_slippage_and_spread(is_buy=False)` | `paper_service.py` | Harga jual < mid saat spread > 0 |
| 3.4 | Sell ditolak jika harga live tidak tersedia (hapus fallback ke avg entry / harga lama) | `paper_service.py:440` | Tidak ada eksekusi di harga karangan |
| 3.5 | Konsistensi presisi: semua uang & share `Decimal` 4 desimal; API menerima/mengirim angka sebagai string (atau `condecimal`) bukan `float` | `dashboard.py`, `paper_service.py` | Tidak ada `float()` di jalur kalkulasi |
| 3.6 | Tolak sell di bawah minimum (mis. proceeds < $0.01) | — | Sell 0.0001 share → `400` |
| 3.7 | Samakan sumber harga UI dan eksekusi: kartu weather-events memakai `lastTradePrice`, eksekusi memakai `outcomePrices`. Pilih satu (disarankan best bid/ask dari CLOB atau `outcomePrices`) | `collector.py` | Divergence warning tidak muncul palsu |
| 3.8 | Pastikan market dari kartu weather-events tersedia di `market_snapshots` (collector juga mengumpulkan dari endpoint tag yang sama), atau upsert snapshot on-demand saat order | `collector.py`, `paper_service.py` | Klik Buy di kartu tidak menghasilkan `404` |

---

## Fase 4 — Performa & Operasional

| ID | Tugas | Kriteria selesai |
|---|---|---|
| 4.1 | Index gabungan `(market_id, timestamp DESC)` di `market_snapshots` | Query latest snapshot < 10 ms pada 1 juta baris |
| 4.2 | Tabel/materialized view `market_latest` (upsert per siklus collector) agar dashboard tidak `DISTINCT ON` ke seluruh histori | Render dashboard tidak melambat seiring waktu |
| 4.3 | Retensi snapshot: downsample data > 30 hari (mis. 1 per jam) atau hapus yang tidak terkait posisi/trade | Pertumbuhan tabel terkendali |
| 4.4 | Hilangkan N+1: ambil harga semua posisi dalam satu query | Satu query untuk seluruh posisi |
| 4.5 | Baseline market sintetis hanya di `APP_ENV=development`, dengan flag `is_synthetic=True` yang tidak bisa di-trade | Tidak ada market palsu di DB produksi |
| 4.6 | Collector: paginasi `/markets` (offset) atau pakai endpoint tag weather, bukan hanya 100 market pertama | Cakupan market cuaca lebih lengkap |
| 4.7 | Healthcheck endpoint `/healthz` (cek DB + umur snapshot terakhir) dan healthcheck di compose | Container restart jika collector macet |
| 4.8 | Logging terstruktur untuk order/settlement (JSON) agar mudah diaudit | — |

---

## Fase 5 — Kualitas Kode & Test (paralel)

| ID | Tugas |
|---|---|
| 5.1 | Hapus import fallback tiga lapis (`try/except ImportError`) — gunakan import absolut `app.*` saja; path `app.paper_trading.paper_service` tidak ada |
| 5.2 | `database.py`: parameter pool hanya untuk PostgreSQL (saat ini crash dengan SQLite) |
| 5.3 | Pecah `paper_service.py` (963 baris) menjadi `accounts.py`, `orders.py`, `positions.py`, `markets.py` |
| 5.4 | Pecah JS di `dashboard.html` (±1900 baris) ke file statis terpisah |
| 5.5 | Test integrasi dengan PostgreSQL nyata (testcontainers / service di CI) |
| 5.6 | CI (GitHub Actions): lint (ruff), type check (mypy), pytest, build Docker |
| 5.7 | Rapikan dokumen duplikat (`AUDIT_REPORT.md` & `Paper Trading Feature.md` ada di root dan `docs/`) dan perbarui README agar sesuai kondisi nyata |

---

## 4. Daftar Test Regresi (Definition of Done)

Setiap item berikut harus menjadi test otomatis yang lulus:

- [x] Akun baru: `portfolio_value == INITIAL_BALANCE`, P/L = 0, posisi kosong
- [x] Data akun, posisi, dan trade tetap ada setelah restart proses
- [x] `get_account_status().realized_pnl == get_performance().realized_pnl`
- [x] Order di market dengan `end_date` lewat → ditolak
- [x] Order di snapshot stale → ditolak
- [x] Order melebihi limit eksposur per market (akumulatif) → ditolak
- [x] 20 order paralel → saldo akhir tepat, tidak ada saldo negatif
- [x] Sell dengan side yang tidak dimiliki → `404`
- [x] Sell tanpa harga live → ditolak
- [x] Sell memakai slippage sisi jual
- [x] Market resolve YES → posisi YES WON (payout = shares), posisi NO LOST
- [x] Settlement dijalankan dua kali → tidak dobel
- [x] Deposit mengubah basis ROI
- [x] Endpoint `POST` tanpa token → `401`
- [x] Judul market berisi HTML ter-escape di dashboard

---

## 5. Urutan Eksekusi yang Disarankan

1. **Minggu 1:** Fase 0 (seluruhnya) + mulai Fase 1 (1.1–1.6)
2. **Minggu 2:** Selesaikan Fase 1 + Fase 2
3. **Minggu 3:** Fase 3 + Fase 4
4. **Sepanjang waktu:** Fase 5 dan test regresi

Setelah Fase 2 selesai, jalankan paper trading minimal **2–4 minggu** dengan data bersih sebelum menarik kesimpulan tentang performa strategi.
