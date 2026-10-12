# Copy trade (paper)

Ikut membeli otomatis saat wallet yang diikuti membeli — per wallet yang dipilih, di akun **paper**.
**Terpisah dari Auto Bot**: saklar sendiri, batas risiko sendiri, notifikasi ke chat sendiri, halaman `/copy` sendiri,
dan tidak ikut dihitung di statistik / rekap / batas harian Auto Bot.

## Cara pakai

1. Lacak & ikuti wallet (dashboard → Wallet Tracker, atau `/track <alamat>` lalu `/follow`).
2. Aktifkan copy per wallet: tombol **Copy** + nominal di tabel "Wallet yang dilacak", atau Telegram
   `/copy <wallet> on 5` (mengaktifkan copy juga mengikuti wallet itu).
3. Pastikan copy trade berjalan: halaman `/copy` → **Start copy**, atau Telegram `/copy start` (default menyala,
   `COPY_ENABLED`). Tidak bergantung pada Auto Bot (`/startbot` / `/stopbot`).

## Aturan

- Dicek tiap `WALLET_POLL_SECONDS`. Pembelian wallet (semua kategori, termasuk yang tidak di-alert) disalin sekali per
  wallet + market + outcome, memakai harga order book **terkini** + slippage + fee, lewat batas risiko copy:
  `COPY_MAX_DAILY_USD` (50), `COPY_MAX_OPEN_USD` (100), `COPY_MAX_DAILY_LOSS` (25).
- Dilewati bila: harga sudah naik > `max_slippage` dari harga wallet (default 3¢), harga di luar
  `min_price`–`max_price` (5–95¢), spread terlalu lebar, atau order book tipis. Transaksi yang sudah > `max_delay_min`
  (15 menit) dilewati. Yang dilewati **tidak** dikirim satu per satu ke Telegram (`COPY_NOTIFY_SKIPS=false`); ringkasan
  & daftarnya ada di `/copy` (Telegram: `/copy dilewati`) dan tab **Dilewati** di halaman `/copy`.
- Notifikasi beli/jual copy dikirim ke `TELEGRAM_COPY_CHAT_ID`, atau chat utama (tempat alert wallet) bila kosong —
  bukan grup auto bot.
- Wallet menjual → posisi copy kita di market + sisi yang sama ikut dijual (`mirror_sell`, bisa dimatikan:
  `/copy <wallet> jual off`).
- Halaman `/copy`: pengaturan per wallet (copy, $/trade, slip, rentang harga, maks telat, ikut jual), hasil per wallet,
  riwayat & statistik, kalender PnL, dan daftar yang dilewati.

## Catatan

Analisis 1.000+ market crypto Up/Down: wallet top tidak konsisten dan edge hilang setelah jeda ±1 menit. Copy trade
lebih masuk akal untuk wallet yang edge-nya informasi (cuaca, politik, olahraga) dan market yang bergerak lambat.
Uji di paper minimal beberapa minggu per wallet sebelum mempertimbangkan uang asli.
