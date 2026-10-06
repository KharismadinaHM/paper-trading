# Live trading (uang asli) — BTC & ETH 1 jam

Bot paper tetap berjalan seperti biasa. Bila diaktifkan, setiap sinyal **BTC 1 jam / ETH 1 jam** yang lolos
semua aturan paper (edge ≥ ambang, harga 30–90¢, spread, jendela menit 30–57) juga dikirim sebagai order
**uang asli** ke Polymarket lewat SDK resmi `py-clob-client`. Kode: `app/paper_trading/live_trader.py`.

> Risiko: bot bisa rugi. Hasil paper bukan jaminan hasil nyata (eksekusi, slippage, ukuran order).
> Ini bukan saran finansial.

## 1. Sebelum mulai

1. **Aturan platform.** Pastikan Polymarket mengizinkan trading dari negara Anda **dan** dari lokasi server
   (order dikirim dari VM GCP). Jangan memakai VPN/server lain untuk mengakali pembatasan wilayah.
2. **Wallet khusus bot.** Buat akun/wallet Polymarket baru, terpisah dari wallet utama, dan isi hanya dengan
   jumlah yang siap hilang. Bila server bocor, kerugian maksimal = isi wallet ini.
3. **Dana.** USDC di Polygon lewat deposit Polymarket. Untuk wallet EOA/MetaMask langsung (tipe 0), siapkan
   sedikit POL untuk gas dan lakukan satu trade manual di Polymarket dulu agar izin (allowance) USDC terpasang.

## 2. Isi `.env` di server (jangan pernah lewat chat / git)

```bash
sudo nano /home/kharismadina_muhamad/paper-trading/.env
```

| Variabel | Isi |
|---|---|
| `LIVE_TRADING` | `true` untuk menyalakan |
| `POLY_PRIVATE_KEY` | private key wallet bot (akun email: *Settings → Export private key* di Polymarket) |
| `POLY_FUNDER_ADDRESS` | alamat wallet Polymarket yang tampil di profil (proxy/funder); wajib untuk tipe 1 & 2 |
| `POLY_SIGNATURE_TYPE` | `1` akun email/Magic · `2` browser wallet (MetaMask lewat Polymarket) · `0` EOA langsung |
| `LIVE_STRATEGIES` | default `btc,eth` (BTC & ETH 1 jam) |
| `LIVE_ORDER_USD` | nominal per order (minimal $1, maks `LIVE_MAX_ORDER_USD` dan $100 di kode) |
| `LIVE_MAX_DAILY_USD` | total belanja live per hari (WIB) |
| `LIVE_MAX_DAILY_LOSS` | bot berhenti hari itu bila rugi terealisasi ≥ ini |
| `LIVE_MAX_OPEN_USD` | total posisi live yang belum resolve |
| `LIVE_MAX_SLIPPAGE` | batas harga = ask saat sinyal + ini (default 2¢) |
| `LIVE_DRY_RUN` | `true` = order disusun & dicatat tanpa dikirim |

Batasi akses file: `sudo chmod 600 .env`. Lalu muat ulang:

```bash
sudo docker compose -f docker-compose.prod.yml up -d --build
```

## 3. Cara kerja order

- **Market order FOK** (isi penuh atau batal) senilai `LIVE_ORDER_USD`.
- **Batas harga** = min(ask saat sinyal + `LIVE_MAX_SLIPPAGE`, harga tertinggi yang edge-nya masih
  ≥ `BTC_MIN_EDGE` setelah fee), dibulatkan ke bawah ke 1¢. Bila harga sudah lari, order **tidak terisi**
  dan tidak ada uang keluar (tercatat `rejected`).
- Satu order per market. Cek saldo USDC sebelum setiap order.
- Notifikasi ke grup auto trade: `💵 LIVE BUY (uang asli) · 🟠 BTC · 1 JAM` (harga isi, nominal, batas).
  Masalah (saldo kurang, batas harian, error) dikabarkan sekali per jenis per hari.
- Hasil WIN/LOSS & PnL nyata diisi otomatis setelah market resolve.

## 4. Mengendalikan

| Cara | Efek |
|---|---|
| `/live` | status, saldo USDC, pemakaian hari ini, PnL, order terbaru |
| `/livestop` · tombol **Jeda** di `/autobot` | hentikan order live baru (paper tetap jalan) |
| `/livestart` · tombol **Lanjutkan** | nyalakan lagi |
| `/stopbot` | hentikan seluruh auto trader (paper & live) |
| `LIVE_TRADING=false` + restart | matikan total |

## 5. Klaim kemenangan

Saham yang menang **tidak otomatis menjadi USDC**. Buka Polymarket → Portfolio → **Claim** secara berkala;
tanpa itu saldo USDC habis dan bot berhenti dengan pesan "saldo USDC kurang".

## 6. Mengevaluasi

Bandingkan `/live` (PnL nyata, harga isi vs batas) dengan `/autostats` (paper). Bila harga isi rutin lebih
mahal dari harga paper atau banyak order `rejected` (harga lari), edge di dunia nyata kemungkinan hilang.
