# 🌡️ Riset Jam Puncak Suhu Harian per Kota

Dokumen ini menjelaskan dasar penentuan **jam puncak suhu tertinggi/terendah** yang dipakai fitur rekomendasi (`app/paper_trading/weather_peaks.py`), beserta hasil riset untuk setiap kota market suhu Polymarket.

## 1. Dasar ilmiah

| Temuan | Implikasi |
|---|---|
| Suhu maksimum terjadi **beberapa jam setelah solar noon** (matahari di titik tertinggi) karena *thermal lag*: permukaan terus menerima panas lebih banyak daripada yang dilepas hingga sore. Umumnya **2–5 sore** waktu lokal. | Puncak tertinggi dihitung dari solar noon + lag. |
| Wilayah **pesisir** cenderung memuncak lebih awal (angin laut), **pedalaman/kering** lebih lambat, **tropis lembap** lebih awal (awan & hujan sore meredam pemanasan). | Lag berbeda per kota → diukur dari data, bukan disamaratakan. |
| Suhu minimum umumnya terjadi **sekitar/sesaat setelah matahari terbit** (malam terus kehilangan panas sampai matahari kembali memanaskan). | Puncak terendah dihitung dari jam matahari terbit + lag. |
| **Jam dinding ≠ jam matahari**: posisi kota di dalam zona waktunya dan DST menggeser solar noon. Madrid (zona CET + DST) solar noon ≈ 14:00; di China yang memakai satu zona waktu, solar noon Chengdu ≈ 1 jam lebih lambat dari Shanghai. | Solar noon dihitung astronomis dari **bujur kota**, lalu dikonversi ke zona waktu lokal (termasuk DST). |
| Polymarket menyelesaikan market suhu dari stasiun cuaca (umumnya bandara, mis. Changi untuk Singapore) berdasarkan **hari kalender lokal**. | Koordinat memakai lokasi stasiun/bandara; hari = tanggal lokal. |

Sumber: [Wikipedia – Diurnal temperature variation](https://en.wikipedia.org/wiki/Diurnal_temperature_variation), [Encyclopedia.com – Diurnal cycles](https://www.encyclopedia.com/science/encyclopedias-almanacs-transcripts-and-maps/diurnal-cycles-0), [UMN Geog 1414 – Air Temperature](https://www.d.umn.edu/~tzhu/geog1414/lecture3.htm), [Sensibo – hottest time of day](https://sensibo.com/blogs/articles/what-is-the-hottest-time-of-the-day), [timeanddate – China one time zone](https://www.timeanddate.com/time/china/one-time-zone.html), [time.is – Madrid](https://time.is/Madrid), [wethr.net – market resolution](https://wethr.net/market-resolution), halaman aturan market Polymarket (mis. [Singapore](https://polymarket.com/event/highest-temperature-in-singapore-on-september-24-2026)).

## 2. Metode

1. **Data**: suhu 2 m per jam dari [Open-Meteo Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api) (reanalisis ERA5), 90 hari terakhir, di koordinat stasiun tiap kota.
2. Untuk setiap hari kalender lokal: catat jam suhu tertinggi & terendah, lalu hitung
   - `lag_max` = jam suhu tertinggi − solar noon hari itu
   - `lag_min` = jam suhu terendah − jam matahari terbit hari itu
3. Lag **tipikal** per kota disimpan di `app/paper_trading/peak_calibration.json` = *modus yang dihaluskan*: titik dengan jumlah hari terbanyak dalam rentang ±30 menit, lalu dirata-rata. Median juga disimpan sebagai pembanding.

   Mengapa bukan median? Di banyak kota Asia Timur saat musim hujan, hari berawan/hujan memiliki puncak sebelum siang sehingga median tertarik 1–2 jam lebih awal (mis. Taipei: median +0,1 jam vs tipikal +2,0 jam setelah solar noon). Dengan median, 18 dari 59 kota memiliki jam "paling sering" di luar jendela puncak; dengan modus yang dihaluskan: 0 dari 59 (suhu tertinggi) dan 1 dari 59 (suhu terendah).
4. Saat aplikasi berjalan, untuk setiap tanggal:
   - pusat puncak tertinggi = solar noon tanggal itu + lag tipikal `lag_max_hours`
   - pusat puncak terendah = matahari terbit tanggal itu + lag tipikal `lag_min_hours`
   - jendela puncak = pusat ± 30 menit (`TEMP_PEAK_DURATION_HOURS`), dibulatkan ke 15 menit
   - **rekomendasi = 2 jam s/d 1 jam sebelum awal puncak** (`RECOMMENDATION_LEAD_HOURS`, `RECOMMENDATION_WINDOW_HOURS`)

   Karena berbasis posisi matahari, pergeseran musim dan DST ikut terhitung otomatis.
5. Solar noon & sunrise dihitung dengan persamaan NOAA (`app/paper_trading/solar.py`); selisih ≤ 2 menit terhadap Open-Meteo pada New York, Madrid, Helsinki, Hong Kong, dan Sydney.

Menjalankan ulang riset (disarankan tiap pergantian musim):

```bash
python scripts/research_peak_hours.py --days 90
```

Koreksi manual per kota (jam **awal** puncak lokal) tetap bisa lewat `.env`:

```ini
TEMP_PEAK_HOUR_OVERRIDES={"Hong Kong": {"highest": 14, "lowest": 6}}
```

## 3. Hasil untuk 26 September 2026

Periode data: 24 Jun – 21 Sep 2026 (±89 hari). Kolom "paling sering" = jam (lokal, periode musim panas) yang paling sering menjadi puncak beserta persentase harinya; kolom "puncak" sudah disesuaikan ke posisi matahari tanggal 26 September.

| Kota | Tertinggi: puncak | Rekomendasi | Paling sering (data) | Terendah: puncak | Rekomendasi | Paling sering (data) |
|---|---|---|---|---|---|---|
| Amsterdam | 13:15–14:15 CEST | 11:15–12:15 | 14:00 (24%) | 07:15–08:15 | 05:15–06:15 | 06:00 (36%) |
| Ankara | 15:15–16:15 +03 | 13:15–14:15 | 16:00 (47%) | 06:00–07:00 | 04:00–05:00 | 06:00 (75%) |
| Atlanta | 15:15–16:15 EDT | 13:15–14:15 | 16:00 (34%) | 07:30–08:30 | 05:30–06:30 | 07:00 (47%) |
| Austin | 15:15–16:15 CDT | 13:15–14:15 | 16:00 (55%) | 06:45–07:45 | 04:45–05:45 | 07:00 (57%) |
| Beijing | 14:15–15:15 CST | 12:15–13:15 | 15:00 (40%) | 05:30–06:30 | 03:30–04:30 | 05:00 (43%) |
| Buenos Aires | 14:15–15:15 -03 | 12:15–13:15 | 15:00 (36%) | 06:15–07:15 | 04:15–05:15 | 08:00 (31%) |
| Busan | 13:15–14:15 KST | 11:15–12:15 | 14:00 (35%) | 05:45–06:45 | 03:45–04:45 | 06:00 (31%) |
| Cape Town | 13:15–14:15 SAST | 11:15–12:15 | 14:00 (55%) | 06:00–07:00 | 04:00–05:00 | 08:00 (29%) |
| Chengdu | 15:15–16:15 CST | 13:15–14:15 | 16:00 (31%) | 06:15–07:15 | 04:15–05:15 | 06:00 (49%) |
| Chicago | 14:15–15:15 CDT | 12:15–13:15 | 15:00 (22%) | 06:30–07:30 | 04:30–05:30 | 06:00 (38%) |
| Chongqing | 14:15–15:15 CST | 12:15–13:15 | 15:00 (33%) | 06:00–07:00 | 04:00–05:00 | 06:00 (52%) |
| Dallas | 15:15–16:15 CDT | 13:15–14:15 | 16:00 (47%) | 07:00–08:00 | 05:00–06:00 | 07:00 (81%) |
| Denver | 14:15–15:15 MDT | 12:15–13:15 | 15:00 (27%) | 06:15–07:15 | 04:15–05:15 | 06:00 (80%) |
| Guangzhou | 12:15–13:15 CST | 10:15–11:15 | 13:00 (24%) | 05:45–06:45 | 03:45–04:45 | 06:00 (42%) |
| Helsinki | 14:15–15:15 EEST | 12:15–13:15 | 15:00 (31%) | 06:45–07:45 | 04:45–05:45 | 05:00 (27%) |
| Hong Kong | 13:15–14:15 HKT | 11:15–12:15 | 14:00 (36%) | 05:45–06:45 | 03:45–04:45 | 06:00 (33%) |
| Houston | 15:15–16:15 CDT | 13:15–14:15 | 16:00 (39%) | 06:45–07:45 | 04:45–05:45 | 07:00 (54%) |
| Istanbul | 14:15–15:15 +03 | 12:15–13:15 | 15:00 (78%) | 03:00–04:00 | 01:00–02:00 | 03:00 (37%) |
| Jeddah | 11:15–12:15 +03 | 09:15–10:15 | 12:00 (29%) | 05:45–06:45 | 03:45–04:45 | 06:00 (78%) |
| Jinan | 12:15–13:15 CST | 10:15–11:15 | 13:00 (38%) | 05:30–06:30 | 03:30–04:30 | 05:00 (44%) |
| Karachi | 12:15–13:15 PKT | 10:15–11:15 | 13:00 (46%) | 03:45–04:45 | 01:45–02:45 | 04:00 (54%) |
| Kuala Lumpur | 13:15–14:15 +08 | 11:15–12:15 | 14:00 (64%) | 06:15–07:15 | 04:15–05:15 | 07:00 (67%) |
| London | 15:15–16:15 BST | 13:15–14:15 | 16:00 (33%) | 06:30–07:30 | 04:30–05:30 | 06:00 (52%) |
| Los Angeles | 11:15–12:15 PDT | 09:15–10:15 | 12:00 (39%) | 05:00–06:00 | 03:00–04:00 | 05:00 (62%) |
| Lucknow | 13:45–14:45 IST | 11:45–12:45 | 14:00 (35%) | 04:30–05:30 | 02:30–03:30 | 04:00 (38%) |
| Madrid | 16:15–17:15 CEST | 14:15–15:15 | 17:00 (53%) | 08:00–09:00 | 06:00–07:00 | 08:00 (71%) |
| Manila | 13:15–14:15 PST | 11:15–12:15 | 14:00 (30%) | 04:30–05:30 | 02:30–03:30 | 05:00 (22%) |
| Mexico City | 14:15–15:15 CST | 12:15–13:15 | 15:00 (40%) | 04:45–05:45 | 02:45–03:45 | 05:00 (52%) |
| Miami | 13:15–14:15 EDT | 11:15–12:15 | 14:00 (47%) | 06:45–07:45 | 04:45–05:45 | 07:00 (28%) |
| Milan | 15:15–16:15 CEST | 13:15–14:15 | 16:00 (31%) | 06:45–07:45 | 04:45–05:45 | 06:00 (43%) |
| Moscow | 14:15–15:15 MSK | 12:15–13:15 | 15:00 (52%) | 06:00–07:00 | 04:00–05:00 | 05:00 (34%) |
| Munich | 15:15–16:15 CEST | 13:15–14:15 | 16:00 (39%) | 06:45–07:45 | 04:45–05:45 | 06:00 (51%) |
| New York City | 14:15–15:15 EDT | 12:15–13:15 | 15:00 (27%) | 06:15–07:15 | 04:15–05:15 | 06:00 (53%) |
| Panama City | 12:15–13:15 EST | 10:15–11:15 | 13:00 (46%) | 05:30–06:30 | 03:30–04:30 | 06:00 (40%) |
| Paris | 16:15–17:15 CEST | 14:15–15:15 | 17:00 (40%) | 07:15–08:15 | 05:15–06:15 | 06:00 (44%) |
| Qingdao | 13:15–14:15 CST | 11:15–12:15 | 14:00 (31%) | 05:15–06:15 | 03:15–04:15 | 05:00 (42%) |
| San Francisco | 13:15–14:15 PDT | 11:15–12:15 | 14:00 (57%) | 05:00–06:00 | 03:00–04:00 | 05:00 (20%) |
| Sao Paulo | 14:15–15:15 -03 | 12:15–13:15 | 15:00 (48%) | 05:30–06:30 | 03:30–04:30 | 07:00 (31%) |
| Seattle | 16:15–17:15 PDT | 14:15–15:15 | 17:00 (54%) | 06:45–07:45 | 04:45–05:45 | 06:00 (52%) |
| Seoul (Incheon) | 14:15–15:15 KST | 12:15–13:15 | 15:00 (44%) | 05:45–06:45 | 03:45–04:45 | 06:00 (30%) |
| Shanghai | 13:15–14:15 CST | 11:15–12:15 | 14:00 (29%) | 05:00–06:00 | 03:00–04:00 | 05:00 (39%) |
| Shenzhen | 13:15–14:15 CST | 11:15–12:15 | 14:00 (39%) | 05:45–06:45 | 03:45–04:45 | 06:00 (43%) |
| Singapore | 13:15–14:15 +08 | 11:15–12:15 | 14:00 (61%) | 07:15–08:15 | 05:15–06:15 | 08:00 (19%) |
| Taipei | 13:15–14:15 CST | 11:15–12:15 | 14:00 (25%) | 05:00–06:00 | 03:00–04:00 | 05:00 (27%) |
| Tel Aviv | 14:15–15:15 IDT | 12:15–13:15 | 15:00 (54%) | 06:00–07:00 | 04:00–05:00 | 06:00 (80%) |
| Tokyo | 14:15–15:15 JST | 12:15–13:15 | 15:00 (31%) | 05:00–06:00 | 03:00–04:00 | 05:00 (35%) |
| Toronto | 14:15–15:15 EDT | 12:15–13:15 | 15:00 (37%) | 06:45–07:45 | 04:45–05:45 | 06:00 (35%) |
| Warsaw | 15:15–16:15 CEST | 13:15–14:15 | 16:00 (33%) | 06:15–07:15 | 04:15–05:15 | 05:00 (44%) |
| Wellington | 11:15–12:15 NZST | 09:15–10:15 | 12:00 (27%) | 05:15–06:15 | 03:15–04:15 | 06:00 (20%) |
| Wuhan | 14:15–15:15 CST | 12:15–13:15 | 15:00 (31%) | 05:45–06:45 | 03:45–04:45 | 06:00 (40%) |
| Zhengzhou | 14:15–15:15 CST | 12:15–13:15 | 15:00 (31%) | 05:45–06:45 | 03:45–04:45 | 06:00 (45%) |

### Pengamatan

- **Puncak sore paling lambat**: Madrid, Paris, Seattle (≈ 16:00–17:00) — solar noon mereka sudah ≈ 13:30–14:00 karena zona waktu + DST.
- **Puncak paling awal**: kota pesisir dengan angin laut kuat — Los Angeles (LAX), Dubai, Jeddah, Jakarta, Wellington (≈ 11:00–13:00).
- **Tropis lembap** (Singapore, Kuala Lumpur): lag kecil (≈ 1 jam setelah solar noon) karena awan/hujan sore.
- **Suhu terendah** hampir selalu di sekitar matahari terbit; pengecualian kota pesisir (Karachi, Mumbai, San Francisco, Los Angeles) cenderung 1–2 jam **sebelum** matahari terbit, dan **Istanbul** ±3 jam sebelumnya (paling sering 03:00) — kemungkinan pengaruh Laut Marmara atau grid ERA5 yang tercampur laut; kandidat verifikasi/override.
- Distribusi jam puncak di banyak kota cukup lebar (20–40% hari di jam yang paling sering). Hari hujan/berawan bisa memuncak jauh lebih awal — itu risiko nyata untuk market suhu tertinggi di musim hujan.

## 4. Keterbatasan

- ERA5 adalah grid ±25 km; di kota pesisir sel grid bisa tercampur laut sehingga jamnya sedikit berbeda dari stasiun bandara. Kota yang dirasa meleset bisa dikoreksi lewat `TEMP_PEAK_HOUR_OVERRIDES`.
- Data 90 hari musim panas; riset ulang disarankan tiap pergantian musim karena pola awan/angin musiman berubah.
- Hari dengan front dingin/hujan bisa memiliki puncak di luar pola (mis. suhu terendah tengah malam).

## Pembaruan: kalibrasi dari stasiun resolusi (Okt 2026)

Kalibrasi sekarang dihitung dari **observasi METAR di stasiun resolusi tiap market** (bandara; kode ICAO
dari deskripsi market; arsip IEM ASOS, 60 hari), bukan grid Open-Meteo di pusat kota:

    python scripts/research_peak_hours.py --source station --days 60
    python scripts/research_peak_hours.py --source station --days 60 --only-missing   # lanjutkan yang gagal

- 47 dari 59 kota memakai data stasiun (`"source": "station:ICAO"`); Hong Kong (HKO, tidak ada di arsip
  METAR), Jinan (data stasiun terlalu berlubang) dan kota yang gagal diambil tetap memakai hasil lama.
- Waktu ekstrem harian memakai **titik tengah** bacaan bernilai ekstrem: METAR dibulatkan ke derajat
  bulat sehingga nilai ekstrem sering bertahan beberapa jam; mengambil bacaan pertama membuat jam
  ekstrem (terutama suhu terendah) bergeser terlalu awal.
- IEM memutus request besar; data diambil per potongan 10 hari dengan jeda dan retry.

Validasi (backtest 17–26 Sep, 490 event tertinggi / 474 terendah, metrik titik tengah yang sama):

| | Grid lama | Stasiun |
|---|---|---|
| Tertinggi: jam ekstrem aktual dalam [−1, +2] jam dari awal puncak perkiraan | 70% | **78%** |
| Tertinggi: median selisih | +0.8 jam | **+0.3 jam** |
| Terendah: dalam [−1, +2] jam | 49% | **54%** |

Periode validasi sebagian tumpang tindih dengan periode riset, jadi angka cenderung sedikit optimistis.
