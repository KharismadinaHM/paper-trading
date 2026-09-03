# 📱 Panduan Menghubungkan Bot Telegram (Existing Bot)

Dokumen ini menjelaskan langkah demi langkah untuk menghubungkan bot Telegram yang sudah Anda miliki (*existing bot*) ke dalam sistem **Polymarket Weather Paper Trading**.

---

## 📋 Ikhtisar Kebutuhan

Sistem membutuhkan 2 variabel lingkungan (*environment variables*) di file `.env`:
1. **`TELEGRAM_BOT_TOKEN`**: Token autentikasi API bot Telegram Anda.
2. **`TELEGRAM_CHAT_ID`**: ID chat tujuan pengiriman notifikasi (bisa akun pribadi atau grup Telegram).

---

## 🛠️ Langkah 1: Mengambil Token Bot Existing dari @BotFather

Jika Anda sudah pernah membuat bot tetapi lupa atau ingin menyalin kembali tokennya:

1. Buka aplikasi Telegram dan cari **`@BotFather`** (dengan centang biru verifikasi).
2. Kirim pesan: `/mybots`
3. Pilih bot yang ingin Anda gunakan dari daftar tombol yang muncul.
4. Klik tombol **API Token**.
5. Salin token API yang ditampilkan.
   - *Format contoh token*: `7123456789:AAFs5example_token_abcdef12345`

---

## 🔍 Langkah 2: Mendapatkan Chat ID & Mengaktifkan Bot

> [!IMPORTANT]
> **Wajib Dilakukan:** Sebelum bot dapat mengirimkan pesan kepada Anda, Anda **harus membuka obrolan dengan bot tersebut dan menekan tombol START** (atau ketik `/start`). Telegram melarang bot memulai obrolan ke pengguna duluan untuk mencegah spam.

### Opsi A: Notifikasi ke Akun Pribadi (Private Chat)

Pilih salah satu cara termudah berikut untuk mengetahui Chat ID akun Anda:

#### Cara 1: Menggunakan Bot Helper (Paling Cepat)
1. Cari bot **`@userinfobot`** atau **`@getidsbot`** di Telegram.
2. Klik tombol **Start** (atau kirim pesan apa saja).
3. Bot akan membalas dengan data Anda. Catat angka di bagian **`Id:`** (contoh: `123456789`).

#### Cara 2: Menggunakan Browser via API Telegram
1. Buka obrolan dengan bot Anda, lalu ketik pesan bebas (misal: "Halo").
2. Buka peramban (browser) dan akses URL berikut (ganti `<BOT_TOKEN>` dengan token bot Anda):
   ```text
   https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
   ```
3. Cari properti `"chat":{"id": 123456789, ...}` di dalam respons JSON. Angka tersebut adalah `TELEGRAM_CHAT_ID` Anda.

---

### Opsi B: Notifikasi ke Grup Telegram

Jika Anda ingin notifikasi trading masuk ke grup tim/komunitas:

1. Buat atau buka grup Telegram yang diinginkan.
2. Tambahkan (*invite*) bot Anda ke dalam grup tersebut.
3. Jadikan bot sebagai **Administrator** grup (agar dapat mengirim pesan tanpa terhalang pembatasan izin).
4. Kirim sembarang pesan di dalam grup (misal: `tes bot`).
5. Buka browser dan akses:
   ```text
   https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
   ```
6. Cari bagian `"chat":{"id": -1001234567890, "title": "Nama Grup", ...}`.
7. Salin angka ID tersebut.
   > [!NOTE]
   > Chat ID grup **selalu diawali tanda minus (`-`)** atau **`-100`**. Tanda minus **wajib diikutsertakan** saat menyalin ke `.env`!

---

## ⚙️ Langkah 3: Masukkan Konfigurasi ke File `.env`

1. Buka file `.env` di root direktori proyek `paper-trading`.
2. Perbarui baris `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID`:

```env
# Telegram Bot Integration
TELEGRAM_BOT_TOKEN=7123456789:AAFs5example_token_abcdef12345
TELEGRAM_CHAT_ID=123456789
```

*(Jika menggunakan grup, contohnya: `TELEGRAM_CHAT_ID=-1001234567890`)*

Simpan file `.env`.

---

## 🚀 Langkah 4: Uji Coba Koneksi (Testing)

Sistem telah menyediakan perintah khusus untuk memverifikasi koneksi Telegram secara instan.

Jalankan perintah berikut di terminal:

```bash
# Pastikan virtual environment aktif jika menggunakan venv
source .venv/bin/activate

# Jalankan test telegram via CLI
python -m app.paper test-telegram
```

### Hasil yang Diharapkan:
- Terminal akan menampilkan:
  ```text
  Mengirim pesan uji coba ke Telegram...
  ✓ Berhasil! Notifikasi uji coba telah terkirim ke Telegram.
  ```
- Bot akan mengirimkan pesan ke Telegram Anda:
  ```text
  🔔 [TEST] Paper Trading System berhasil terhubung ke bot Telegram Anda!
  ```

---

## 📊 Format Notifikasi Paper Trading yang Diterima

Setelah terhubung, sistem akan otomatis mengirimkan pembaruan real-time:

### 1. Sinyal Beli (Paper Buy)
```text
🟢 PAPER BUY
Market: Will Tokyo temperature exceed 30°C on Sep 10?
Side: YES
Entry: $0.65
Position: $1.00
Shares: 1.5385
Expected Peak: 14:00
Reason: Weather forecast deviation > 2.5°C
```

### 2. Penyelesaian Transaksi (Paper Trade Settled)
```text
✅ PAPER TRADE SETTLED
Entry: $0.65
Result: WIN
Gross P/L: +$0.35
Fees: $0.00
Net P/L: +$0.35
Balance: $20.00 → $20.35
```

---

## 🤖 Langkah 5: Menjalankan Bot Interaktif (Command Listener)

Agar bot dapat merespon perintah interaktif dari chat Telegram (seperti `/start`, `/status`, dll), sistem menyediakan background listener:

```bash
# Jalankan listener bot
python -m app.paper bot
```

Terminal akan menampilkan:
```text
🤖 Memulai interactive Telegram bot listener...
🤖 Telegram Bot Polling aktif!
🔒 Terkunci untuk Chat ID: 123456789
Tekan Ctrl+C untuk menghentikan.
```

---

## 💬 Daftar Perintah Bot Telegram

Setelah bot listener aktif, Anda dapat mengetikkan perintah berikut langsung di ruang obrolan bot Telegram:

| Perintah | Fungsi / Keterangan |
| :--- | :--- |
| **`/start`** atau **`/help`** | Menampilkan salam pembuka dan daftar semua perintah yang tersedia. |
| **`/status`** | Menampilkan ringkasan saldo akun, dana terinvestasi, Realized P/L, Win Rate, dan jumlah posisi terbuka. |
| **`/positions`** | Menampilkan daftar seluruh posisi terbuka beserta harga masuk (*entry*), ukuran posisi (*shares*), dan floating P/L. |
| **`/trades`** | Menampilkan 5 riwayat transaksi terakhir yang telah selesai (*settled*), status WIN/LOSS, dan Net P/L. |
| **`/performance`** | Menampilkan ringkasan metrik statistik (Win Rate %, ROI %, Realized P/L, Max Drawdown). |
| **`/ping`** | Memeriksa apakah engine server Paper Trading aktif dan terhubung. |

> 🔒 **Catatan Keamanan**: Bot hanya akan merespon Chat ID yang terdaftar pada `TELEGRAM_CHAT_ID` di file `.env` Anda untuk mencegah pihak tidak berwenang melihat data trading Anda.

---

## 🔘 Opsional: Menampilkan Menu Autocomplete di Telegram

Agar saat mengetik tanda slash `/` di Telegram langsung muncul tombol menu daftar perintah, daftarkan daftar perintah tersebut ke `@BotFather`:

1. Buka chat dengan **`@BotFather`**.
2. Kirim perintah: `/setcommands`
3. Pilih bot Anda.
4. Kirim teks daftar perintah berikut sekaligus:
   ```text
   start - Tampilkan bantuan & menu bot
   status - Cek saldo, realized P/L, & win rate
   positions - Cek posisi trading yang sedang terbuka
   trades - Riwayat 5 transaksi terakhir yang selesai
   performance - Analisis metrik performa trading
   ping - Cek status koneksi bot
   help - Bantuan penggunaan
   ```
5. `@BotFather` akan membalas: `Success! Command list updated.`
6. Buka bot Anda di Telegram, tombol menu pintasan perintah di samping kolom teks input sekarang sudah aktif!

---

## ⚠️ Troubleshooting & FAQ

| Kendala / Pesan Error | Penyebab | Solusi |
| :--- | :--- | :--- |
| `401 Unauthorized` | Token bot salah atau telah di-revoke. | Periksa kembali token dengan `/mybots` di `@BotFather`. |
| `400 Bad Request: chat not found` | Anda belum pernah menekan `/start` di bot, atau Chat ID salah. | Buka chat bot di Telegram, klik **START** / ketik `/start`. |
| `403 Forbidden: bot was blocked by the user` | Bot pernah diblokir di akun Anda. | Buka profil bot di Telegram, pilih *Unblock Bot*. |
| ID Grup tidak bekerja | Tanda minus (`-`) terlewat. | Pastikan ID grup menyertakan tanda minus (misal `-100192837465`). |
| Respon `getUpdates` kosong (`{"ok":true,"result":[]}`) | Belum ada pesan baru setelah bot dibuat/dijalankan. | Kirim pesan baru ke bot atau ke grup, lalu refresh halaman `getUpdates`. |
| Bot tidak merespon saat diketik `/status` | Command listener (`python -m app.paper bot`) belum dijalankan di terminal. | Jalankan `python -m app.paper bot` di terminal. |
| Balasan "Akses Ditolak" | Chat ID pengirim tidak sama dengan `TELEGRAM_CHAT_ID` di `.env`. | Pastikan `TELEGRAM_CHAT_ID` di `.env` sesuai dengan Chat ID akun pengirim. |
