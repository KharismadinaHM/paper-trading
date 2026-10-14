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

## Simulasi Monte Carlo (sumber utama peluang)

`hk_simulation.py` menjalankan ±1.000 skenario suhu per jam untuk sisa hari (dan besok):
122 anggota ensemble Open-Meteo (ECMWF ENS, GFS ENS, ICON EPS — suhu & hujan konsisten secara fisika) + 5 model
deterministik, masing-masing dikoreksi bacaan HKO terakhir; noise AR(1) sebesar error historis proyeksi; kejutan hujan
(nowcast/peringatan) yang memudar perlahan; geser sebagian ke prakiraan resmi HKO; bias kalibrasi; dan porsi kecil
klimatologi 30 tahun (±3 hari, digeser anomali 5 hari terakhir) yang menyusut saat puncak mendekat. Peluang bracket =
proporsi skenario; bracket yang sudah mustahil tetap 0.

Aturan masuk: hanya bila puncak (max) / titik terendah (min) tinggal ≤ `HK_LEAD_HOURS` (2 jam) atau sudah lewat
(nilai terukur bertahan di ≥ 50% skenario), dan harga ≥ `HK_MIN_PRICE` (15¢). 10 Okt bot membeli bracket 6.7¢ jam 09:04
(model 23% vs pasar 5%) dan kalah — kedua aturan ini akan menolaknya.

## Profil bias stasiun & pengaman (14 Okt)

3 kalah beruntun (10–12 Okt, 13 Okt menyusul): model meremehkan puncak ±1°C, pasar (31°C) benar setiap kali.
Data 30 Sep–13 Okt: max HKO − model **+0.66°C** (13/14 hari), min **+2.2°C** (14/14). Profil per jam HKT (14 hari):
10–14 ≈ 0, **15–19 +1.0…+1.3** (HKO tetap panas saat model mendingin), **malam +1.4…+1.8** (pulau panas kota).
- `hko_hourly.station_bias_profile()`: bias per jam (ditarik ke 0 bila sampel sedikit) + selisih puncak/lembah 10 menit.
  Koreksi jam-jam jauh kini meluruh ke profil ini, bukan ke 0 — di proyeksi per jam, ensemble, simulasi, dan forecast.
- Lewati YES bila peluang model − pasar > `HK_MAX_DISAGREE` (30 poin) — keempat trade di atas tertolak.
- `HK_MODEL_WEIGHT` 0.6 → 0.4; simulasi < 20 jalur model (ensemble gagal) tidak dipakai membeli; ensemble terakhir yang
  berhasil (≤ 6 jam) dipakai saat pengambilan gagal.

## Jual / switch posisi (hk_positions.py)

Posisi HK terbuka dinilai ulang tiap siklus: nilai jual = VWAP bid terkini untuk seluruh share − fee − slippage (NO: 1 − ask
YES); jual bila nilai jual − peluang menahan (model+pasar) ≥ `HK_EXIT_EDGE` (5¢), posisi ≥ `HK_SWITCH_COOLDOWN_MIN` (20)
menit, dan exit hari itu < `HK_MAX_SWITCHES` (2). NO pada bracket yang sudah mustahil tidak pernah dijual. Setelah jual
(live), strategi itu boleh membeli lagi hari itu — bukan bracket yang baru dijual (mis. jual 30°C, beli 31°C bila lolos).

**Mode bayangan** (`HK_SWITCH_LIVE=false`, default): hanya dicatat & dikabarkan "👻 HK JUAL (bayangan)". Setelah market
resolve dinilai: menyelamatkan (sisi kita kalah) atau salah langkah (menahan lebih baik), dengan selisih $ — di `/hk` →
Riwayat → "Posisi terbuka & jual/switch". Aktifkan live di Auto Bot → Pengaturan setelah hasil bayangan bagus.

## Sisi NO (hk_max_no / hk_min_no)

Bot membeli NO hanya pada bracket yang **mustahil** oleh angka terukur hari ini (max sudah melewati bracket itu, atau
min sudah di bawahnya) atau yang peluang model YES-nya ≤ `HK_NO_MAX_PROB` (3%). Edge NO = (1 − peluang YES campuran)
− (harga NO + fee) ≥ `HK_NO_MIN_EDGE` (3¢); harga NO ≤ `HK_NO_MAX_PRICE` (97¢). Harga NO dihitung dari bid YES
(order book dua sisi bercermin). Bracket mustahil boleh dibeli kapan saja (setelah `HK_START_HOUR`); yang "hampir
mustahil" mengikuti aturan ≤ 2 jam sebelum puncak. Satu trade NO per jenis per hari. Untung kecil per trade, rugi
penuh bila salah — makanya syaratnya ketat.

## Ensemble, nowcast hujan, dan rezim cuaca

- **Ensemble 5 model** (ECMWF, GFS, ICON, JMA, CMA lewat Open-Meteo), masing-masing dikoreksi bacaan HKO terakhir.
  Rata-ratanya dirata-rata dengan proyeksi per jam; sebarannya (±) menjadi batas bawah ketidakpastian, jadi bila model
  tidak sepakat bot otomatis lebih hati-hati. Juga dipakai untuk model hari besok.
- **Nowcast hujan 2 jam** (grid radar HKO per 30 menit) + **peringatan HKO** (hujan lebat, badai petir, siklon) + cuaca
  terkini. Bila hujan diperkirakan di stasiun: sisa kenaikan max tinggal `AUTOTRADE_HK_RAIN_RISE_KEEP` (40%), min
  diturunkan `AUTOTRADE_HK_RAIN_MIN_DROP` (1°C), ketidakpastian ×1.3.
- **Rezim** saat perkiraan dibuat: `hujan` (hujan diperkirakan / peluang hujan ≥ 60%), `mendung` (awan ≥ 80% 3 jam ke
  depan), `cerah`. Disimpan di tiap sinyal untuk kalibrasi.

Semua terlihat di `/hk` → Forecast (nowcast, peringatan, rezim, tabel ensemble) dan di detail model Overview.

## Kalibrasi otomatis harian

Setiap hari setelah 00:30 HKT (`hk_calibration.py`), dari sinyal 30 hari terakhir:

- **Bias per kelompok jam HKT** (00–06, 06–09, 09–12, 12–15, 15–18, 18–24) untuk max dan min: rata-rata
  (hasil resmi − perkiraan mentah). Ditambahkan ke titik perkiraan berikutnya. Perkiraan mentah disimpan di sinyal
  (`mu_raw`) sehingga koreksi tidak menghitung dirinya sendiri.
- **Penyesuaian per rezim cuaca** (hujan / mendung / cerah): rata-rata residu yang tersisa setelah bias per jam,
  dipakai setelah ≥ 8 hari rezim itu. Ini juga menyetel ulang efek koreksi hujan di atas berdasarkan hasil nyata.
- **Bobot model vs pasar**: bobot dengan log-loss terkecil pada sinyal yang sudah resolve.

Pengaman: dipakai setelah ≥ 10 hari data; ditarik ke 0 / bobot default bila data sedikit; bias maks ±1.5°C;
berubah maks 0.3°C dan 0.1 bobot per hari; tiap hari berbobot sama. Bobot manual di dashboard selalu menang.
Hasil dikabarkan ke grup auto trade. `/kalibrasi` (status) · `/kalibrasi jalankan` · `/kalibrasi reset`; juga di
halaman `/hk` → Overview. Matikan dengan *Hong Kong: kalibrasi otomatis harian* di Auto Bot → Pengaturan.

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
GEMINI_MODEL=gemini-3.8-flash
```

Tanpa key, bot HK tetap berjalan dan ringkasan terkirim dengan bagian model saja.
API key dikirim lewat header `x-goog-api-key`, tidak pernah di URL atau log. Biaya kira-kira 8 ringkasan/hari + pertanyaan.
