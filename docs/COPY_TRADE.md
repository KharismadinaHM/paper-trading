# Copy trade (paper)

Ikut membeli otomatis saat wallet yang diikuti membeli — per wallet yang dipilih, di akun **paper**.

## Cara pakai

1. Lacak & ikuti wallet (dashboard → Wallet Tracker, atau `/track <alamat>` lalu `/follow`).
2. Aktifkan copy per wallet: tombol **Copy** + nominal di tabel "Wallet yang dilacak", atau Telegram
   `/copy <wallet> on 5` (mengaktifkan copy juga mengikuti wallet itu).
3. Centang strategi **copy** di Auto Bot → Pengaturan, dan auto trader harus berjalan (`/startbot`).

## Aturan

- Dicek tiap `WALLET_POLL_SECONDS`. Pembelian wallet (semua kategori, termasuk yang tidak di-alert) disalin sekali per
  wallet + market + outcome, memakai harga order book **terkini** + slippage + fee, lewat risk check auto trader
  (batas harian, posisi terbuka, stop rugi).
- Dilewati (dan dikabarkan sekali) bila: harga sudah naik > `max_slippage` dari harga wallet (default 3¢), harga di luar
  `min_price`–`max_price` (5–95¢), spread terlalu lebar, atau order book tipis. Transaksi yang sudah > `max_delay_min`
  (15 menit) dilewati diam-diam.
- Wallet menjual → posisi copy kita di market + sisi yang sama ikut dijual (`mirror_sell`, bisa dimatikan:
  `/copy <wallet> jual off`).
- Hasil per wallet (jumlah copy, win rate, PnL) di tabel wallet dan `/copy`; total di Auto Bot (filter "Copy trade").

## Catatan

Analisis 1.000+ market crypto Up/Down: wallet top tidak konsisten dan edge hilang setelah jeda ±1 menit. Copy trade
lebih masuk akal untuk wallet yang edge-nya informasi (cuaca, politik, olahraga) dan market yang bergerak lambat.
Uji di paper minimal beberapa minggu per wallet sebelum mempertimbangkan uang asli.
