# Live trading (uang asli) — BTC & ETH (1 jam, 15 menit, 5 menit)

Bot paper tetap berjalan seperti biasa. Bila diaktifkan, setiap sinyal crypto Up/Down dari seri yang dipilih
untuk live (default **BTC 1 jam & ETH 1 jam**) yang lolos aturan sinyal (edge ≥ ambang, harga 30–90¢, spread,
jendela menit masuk) juga dikirim sebagai order **uang asli** ke Polymarket lewat SDK resmi CLOB V2
`py-clob-client-v2` (collateral V2; auto-claim memakai alamat collateral dari config SDK itu).
Kemenangan bisa di-claim otomatis. Kode: `app/paper_trading/live_trader.py`.

> Risiko: bot bisa rugi. Hasil paper bukan jaminan hasil nyata (eksekusi, slippage, ukuran order).
> Ini bukan saran finansial.

## 1. Sebelum mulai

1. **Aturan platform.** Pastikan Polymarket mengizinkan trading dari negara Anda **dan** dari lokasi server
   (order dikirim dari VM GCP). Jangan memakai VPN/server lain untuk mengakali pembatasan wilayah.
2. **Wallet khusus bot.** Buat akun/wallet Polymarket baru, terpisah dari wallet utama, dan isi hanya dengan
   jumlah yang siap hilang. Private key memberi kendali penuh atas wallet: bila server bocor, kerugian
   maksimal = isi wallet ini.
3. **Dana.** USDC di Polygon lewat deposit Polymarket. Untuk wallet EOA/MetaMask langsung (tipe 0), siapkan
   sedikit POL untuk gas dan lakukan satu trade manual di Polymarket dulu agar izin (allowance) USDC terpasang.

## 2. Isi `.env` di server (jangan pernah lewat chat / git)

```bash
sudo nano /home/kharismadina_muhamad/paper-trading/.env
```

| Variabel | Isi |
|---|---|
| `LIVE_TRADING` | `true` untuk menyalakan (saklar utama, hanya dari .env) |
| `POLY_PRIVATE_KEY` | private key wallet bot (akun email: *Settings → Export private key* di Polymarket) |
| `POLY_FUNDER_ADDRESS` | alamat wallet Polymarket yang tampil di profil (proxy/funder); wajib untuk tipe 1 & 2 |
| `POLY_SIGNATURE_TYPE` | `1` akun email/Magic · `2` browser wallet (MetaMask lewat Polymarket) · `0` EOA langsung |
| `LIVE_MAX_ORDER_USD` | batas keras per order (default $25; $100 dikunci di kode) — dashboard tidak bisa melewatinya |
| `POLY_BUILDER_API_KEY` / `_SECRET` / `_PASSPHRASE` | kredensial Builder API untuk auto-claim (lihat §5) |
| `LIVE_STRATEGIES`, `LIVE_ORDER_USD`, `LIVE_MAX_DAILY_USD`, `LIVE_MAX_DAILY_LOSS`, `LIVE_MAX_OPEN_USD`, `LIVE_MAX_SLIPPAGE`, `LIVE_AUTO_CLAIM` | nilai awal; bisa diubah dari dashboard (§4) |
| `LIVE_DRY_RUN` | `true` = order disusun & dicatat tanpa dikirim |

Batasi akses file lalu muat ulang:

```bash
sudo chmod 600 .env && sudo docker compose -f docker-compose.prod.yml up -d --build
```

## 3. Cara kerja order

- **Market order FOK** (isi penuh atau batal) senilai nominal per order.
- **Batas harga** = min(ask saat sinyal + slippage maks, harga tertinggi yang edge-nya masih ≥ `BTC_MIN_EDGE`
  setelah fee), dibulatkan ke bawah ke 1¢. Bila harga sudah lari, order **tidak terisi** dan tidak ada uang
  keluar (tercatat `rejected`).
- Jendela masuk sama dengan paper: 1 jam menit 30–57 · 15 menit menit 7–14 · 5 menit menit 2–4.
- Seri live dipilih terpisah dari strategi paper: live tetap berjalan walau strategi paper seri itu nonaktif.
  `/stopbot` tetap menghentikan semuanya.
- Satu order per market. Cek saldo USDC sebelum setiap order.
- Notifikasi ke grup auto trade: `💵 LIVE BUY (uang asli) · 🔷 ETH · 15 MENIT` (harga isi, nominal, batas).
  Masalah (saldo kurang, batas harian, error) dikabarkan sekali per jenis per hari.
- Hasil WIN/LOSS & PnL nyata diisi otomatis setelah market resolve.

## 4. Mengendalikan & pengaturan

| Cara | Efek |
|---|---|
| `/autobot` → kartu **Live trading** → **Pengaturan live** | pilih seri (BTC/ETH 1 jam, 15 menit, 5 menit), nominal, batas harian, stop rugi, maks terbuka, slippage maks, auto-claim — berlaku langsung |
| `/live` | status, saldo USDC, pemakaian hari ini, PnL, auto-claim, order terbaru |
| `/livestop` · tombol **Jeda** | hentikan order live baru (paper tetap jalan) |
| `/livestart` · tombol **Lanjutkan** | nyalakan lagi |
| `/stopbot` | hentikan seluruh auto trader (paper & live) |
| `LIVE_TRADING=false` + restart | matikan total |

## 5. Auto-claim kemenangan

Saham yang menang **tidak otomatis menjadi USDC**. Bila auto-claim aktif, tiap ±5 menit bot mengambil posisi
menang yang sudah resolve (`redeemable`) di wallet bot dan memanggil `ConditionalTokens.redeemPositions` lewat
**Relayer Polymarket** — tanpa gas, satu transaksi untuk banyak market. Notifikasi: `🪙 AUTO CLAIM · N market`
dengan link Polygonscan.

Syarat:
- Akun email/Magic (tipe 1) atau browser wallet (tipe 2). Akun EOA (tipe 0) tidak didukung → claim manual.
- Kredensial **Builder API**: Polymarket → Settings → **Builder** → buat API key, salin key, secret, passphrase
  ke `POLY_BUILDER_API_KEY`, `POLY_BUILDER_SECRET`, `POLY_BUILDER_PASSPHRASE`.
- Market neg-risk dilewati (tidak terjadi pada BTC/ETH Up/Down) — claim manual.

Bila auto-claim tidak aktif / gagal, claim manual: Polymarket → Portfolio → **Claim**. Tanpa claim, saldo USDC
habis dan bot berhenti dengan pesan "saldo USDC kurang".

## 6. Mengevaluasi

- **Kalender PnL** di `/autobot`: tombol **💵 Live** menampilkan PnL nyata per hari/bulan (tanggal resolve);
  klik tanggal untuk riwayat order live.
- Bandingkan `/live` (PnL nyata, harga isi vs batas) dengan `/autostats` (paper). Bila harga isi rutin lebih
  mahal dari harga paper atau banyak order `rejected` (harga lari), edge di dunia nyata kemungkinan hilang.
