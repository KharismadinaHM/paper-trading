# Bot Hong Kong & AI (Gemini)

Paper trading saja. Market: suhu **tertinggi** (`hk_max`) dan **terendah** (`hk_min`) hari ini di stasiun HK Observatory.

## Cara bot menghitung peluang

1. **Angka terukur hari ini** (bacaan HKO 10 menit): max tidak bisa turun, min tidak bisa naik (hari kalender HKT).
2. **Proyeksi per jam sisa hari** (Open-Meteo dikoreksi bacaan HKO, bias meluruh) → puncak/lembah sisa hari.
3. **Prakiraan resmi HKO**: bila menyebut angka max/min, dirata-rata (`AUTOTRADE_HK_OFFICIAL_WEIGHT`, default 0.5).
4. **Ketidakpastian** dari error historis proyeksi per jarak jam (tabel `station_forecasts` vs bacaan, ≥30 sampel),
   batas bawah 0.3°C; setelah max dianggap final (≥17:00 HKT atau suhu turun ≥1°C dari max) 0.15°C.
5. Suhu akhir = max(terukur, puncak) / min(terukur, lembah); bracket "X°C" = X.0–X.9°C.
6. Peluang untuk edge = pasar + `HK_MODEL_WEIGHT` × (model − pasar) (default 0.6).

Bot membeli YES bracket dengan edge terbesar bila: jam HKT ≥ `HK_START_HOUR` (9), edge ≥ `HK_MIN_EDGE` (8¢),
harga antara `HK_MIN_PRICE` (5¢) dan `MAX_PRICE`, spread ≤ `MAX_SPREAD`, dan belum trade di jenis itu hari ini.
Batas harian/posisi terbuka mengikuti aturan auto trader. Semua pengaturan bisa diubah di dashboard Auto Bot → Pengaturan.

Sinyal dicatat tiap 30 menit (juga bila strategi dimatikan) untuk dinilai dengan `/autostats`, `/autoverdict`, riset.

`knowledge/hk_karakteristik.md` (pola harian, faktor musiman) dipakai sebagai pengetahuan AI dan kerangka pengecekan;
angka peluang tetap dari data.

## AI (Gemini) — bayangan, tidak menentukan pembelian

- Ringkasan otomatis pada `HK_AI_REPORT_HOURS` (default 00, 03, 06, 09, 12, 15, 18, 21) zona `HK_AI_REPORT_TZ` (WIB) ke chat utama.
- `/rangkum` (Telegram) atau tombol **Ringkasan sekarang** (halaman `/hk`): kejadian saat ini. Cache 10 menit; `/rangkum baru` memaksa segar.
- `/tanya <pertanyaan>` atau kotak **Tanya AI** di `/hk`: maks `HK_AI_ASK_PER_HOUR` (20) per jam.
- Setiap ringkasan mencatat peluang per bracket dari AI, model, dan pasar (`hk_forecast_views`). Setelah hari selesai
  dinilai dengan skor Brier (kecil = baik) dan ketepatan bracket teratas. AI baru layak ikut memengaruhi keputusan bila
  skornya konsisten lebih baik daripada model dan pasar.

### Mengaktifkan

Isi di `.env` server (langsung di server, jangan dikirim lewat chat), lalu rebuild container:

```
GEMINI_API_KEY=...        # dari Google AI Studio
GEMINI_MODEL=gemini-2.5-flash
```

Tanpa key, bot HK tetap berjalan dan ringkasan terkirim dengan bagian model saja.
API key dikirim lewat header `x-goog-api-key`, tidak pernah di URL atau log. Biaya kira-kira 8 ringkasan/hari + pertanyaan.
