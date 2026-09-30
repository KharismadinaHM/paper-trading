# Aturan Bot: Auto Paper Trader, Rekomendasi & Alert

Dokumen ini menjelaskan **aturan (rule base)** yang dipakai bot: kapan ia membeli, bagaimana peluang
dihitung, batas risiko, serta aturan rekomendasi dan alert. Bot **tidak memakai machine learning**;
ia memakai aturan eksplisit + model statistik sederhana agar setiap keputusan bisa dijelaskan dan diuji.

> **Paper saja.** Auto trader hanya membeli di akun paper dan tidak pernah mengirim order ke Polymarket.
> Ini alat riset strategi, bukan saran investasi.

Nilai di bawah adalah default dari `.env`. Yang bertanda ✏️ bisa diubah langsung dari dashboard
(section **Auto Paper Trader → Ubah aturan & batas**) dan berlaku tanpa restart.

---

## 1. Aturan inti (semua strategi)

> **Beli hanya jika: peluang model − (harga ask order book + biaya taker) ≥ edge minimum**

| Komponen | Cara dihitung |
|---|---|
| Peluang model | Bergantung strategi (lihat bagian 2–4) |
| Harga ask | **VWAP** — level ask order book CLOB disusuri sesuai ukuran order (bukan harga tengah; harga "50¢" sering berasal dari order book kosong) |
| Biaya taker | `0.07 × p × (1 − p)` per share untuk market yang ber-fee (dibaca per market dari `/fee-rate`); ≈1.75¢ di harga 50¢. Maker tidak membayar |
| Edge | peluang model − (VWAP + biaya) |

Kode: `app/paper_trading/autotrader.py`.

---

## 2. BTC Up/Down (`btc` = 1 jam, `btc15` = 15 menit)

| Aturan | Nilai / isi | Setting |
|---|---|---|
| Resolusi 1 jam | Candle 1H BTC/USDT Binance: Up jika close ≥ open | — |
| Resolusi 15 menit | TWAP Chainlink BTC/USD di akhir rentang ≥ harga awal. Diuji pada 160 market: "close Binance ≥ open Binance" cocok 93.8% (rata-rata sepanjang rentang hanya 85.6%) → dimodelkan sama | — |
| Model peluang | `P(Up) = Φ( ln(harga sekarang / open) / (σ · √menit tersisa) )`, σ = std return 1 menit dari 120 menit terakhir, tanpa asumsi drift | — |
| Waktu masuk 1 jam | menit ke-30 s/d 57, dicek tiap 10 detik | `AUTOTRADE_BTC_WINDOW`, `AUTOTRADE_POLL_SECONDS` |
| Waktu masuk 15 menit | menit ke-7 s/d 14 | `AUTOTRADE_BTC15_WINDOW` |
| Edge minimum ✏️ | 5¢ | `AUTOTRADE_BTC_MIN_EDGE` |
| Harga maksimum ✏️ | 90¢ | `AUTOTRADE_MAX_PRICE` |
| Spread maksimum ✏️ | 5¢ | `AUTOTRADE_MAX_SPREAD` |
| Sisi | Up = YES, Down = NO (sisi dengan edge terbesar) | — |

**Catatan riset:** backtest 14 hari (`scripts/backtest_btc_hourly.py`) menunjukkan model **setara** dengan
pasar setelah waktu data diukur adil — market maker Polymarket mengikuti Binance hampir real-time.
Run pertama sempat menunjukkan "edge" +15–40% yang ternyata artefak (riwayat harga CLOB hanya per menit).

---

## 3. Maker (`maker_btc`, `maker_btc15`)

| Aturan | Nilai / isi | Setting |
|---|---|---|
| Harga limit ✏️ | `floor(P_model − 4¢)`, tidak pernah menyilang ask; tanpa biaya taker | `AUTOTRADE_MAKER_MARGIN` |
| Edge minimum ✏️ | 4¢ (P − limit) | `AUTOTRADE_MAKER_MIN_EDGE` |
| Pemilihan sisi | Edge terbesar; bila sama, sisi dengan peluang model lebih tinggi | — |
| Terisi | Hanya jika ask terbaik turun **di bawah** limit (konservatif: antrian di harga yang sama dianggap tidak terisi) | — |
| Dibatalkan | Bila edge model turun di bawah setengah edge minimum (2¢) | — |
| Kedaluwarsa | Akhir jendela: 1 jam menit 15–50, 15 menit menit 3–12 | `AUTOTRADE_MAKER_WINDOW`, `AUTOTRADE_MAKER15_WINDOW` |
| Anggaran | Order terbuka ikut dihitung sebagai dana terpakai | — |
| Rebate maker | Tidak dihitung (konservatif) | — |

---

## 4. Cuaca (`weather`, `weather_post`) — gabungan waktu + observasi + harga

### Waktu
| Aturan | Isi | Setting |
|---|---|---|
| Jam puncak | Suhu tertinggi: solar noon + jeda khas kota; terendah: matahari terbit + jeda khas kota. Jeda dari riset data historis (`app/paper_trading/peak_calibration.json`, lihat `docs/PEAK_HOURS_RESEARCH.md`); DST & musim ikut terhitung | `TEMP_PEAK_HOUR_OVERRIDES` |
| `weather` | Jendela 2–1 jam sebelum awal jam puncak | `RECOMMENDATION_LEAD_HOURS`, `RECOMMENDATION_WINDOW_HOURS` |
| `weather_post` | Dari awal jam puncak s/d akhir puncak + 3 jam | `AUTOTRADE_WEATHER_POST_HOURS` |
| Kota | 7 kota dengan total volume market suhu terbesar | `TELEGRAM_RECOMMENDATION_TOP_CITIES` |
| Jenis | Tertinggi/terendah sesuai setting | `RECOMMENDATION_KINDS` |

### Perkiraan suhu akhir (di stasiun resolusi market)
| Kota | Sumber |
|---|---|
| Hong Kong | HKO real-time (per 10 menit, 0.1°C). Prakiraan resmi HKO diutamakan ("maximum temperature … NN degrees", atau "very hot" = 33°C), digabung proyeksi tren (laju °/jam sampai akhir jam puncak; setelah puncak sampai batas final, maks 2 jam). Tidak pernah di bawah max terukur. **Max tidak dianggap final sebelum 17:00 HKT** (`HKO_FINAL_HOUR`) kecuali suhu sudah turun ≥ 1°C dari max (`HKO_FINAL_DROP`) |
| Kota lain | Prakiraan per jam Open-Meteo di koordinat stasiun, dikoreksi selisih observasi METAR/NOAA terakhir − prakiraan. Tidak pernah di bawah max terukur (atau di atas min terukur). Bila jam puncak sudah lewat dan suhu tidak naik lagi (tren ≤ +0.1°/jam), max dianggap final |

### Peluang bracket
- Suhu akhir ~ **Normal(perkiraan, σ)**, σ = `0.6°C + 0.3°C × jam menuju akhir puncak` (×1.8 untuk °F) —
  setting `AUTOTRADE_WEATHER_SIGMA_C`.
- Dipotong di angka yang sudah terukur (max tidak bisa turun, min tidak bisa naik).
- Pembulatan bacaan: METAR bulat → bracket X = [X − 0.5, X + 0.5]; HKO 0.1°C → bracket X = [X, X + 1).

### Syarat beli
| Syarat | Nilai | Setting |
|---|---|---|
| Sepakat dengan favorit pasar ✏️ | Bracket paling mungkin menurut model = bracket likuid dengan peluang pasar tertinggi | `AUTOTRADE_WEATHER_REQUIRE_AGREEMENT` |
| Edge minimum ✏️ | 5¢ | `AUTOTRADE_WEATHER_MIN_EDGE` |
| Harga maks ✏️ / spread maks ✏️ | 90¢ / 5¢ | `AUTOTRADE_MAX_PRICE`, `AUTOTRADE_MAX_SPREAD` |
| Likuid | Bracket & event harus punya order book dengan spread wajar | — |

---

## 5. Risk engine (semua strategi) ✏️

| Batas | Default | Setting |
|---|---|---|
| Ukuran per order | $5 | `AUTOTRADE_ORDER_USD` |
| Pembelian per hari (hari WIB) | $50 | `AUTOTRADE_MAX_DAILY_USD` |
| Stop harian | Berhenti hari itu bila rugi terealisasi ≥ $20 | `AUTOTRADE_MAX_DAILY_LOSS` |
| Total posisi terbuka | $100 (termasuk limit order maker terbuka) | `AUTOTRADE_MAX_OPEN_USD` |
| Entri per market | Satu kali; tidak pernah menambah posisi | — |
| Strategi aktif ✏️ | `weather, weather_post, btc, btc15, maker_btc, maker_btc15` | `AUTOTRADE_STRATEGIES` |
| Kill switch | `/stopbot` / tombol Stop di dashboard (status awal `AUTOTRADE_ENABLED`) | — |
| Saldo paper | Order ditolak bila saldo paper tidak cukup (tambah lewat Deposit) | — |

Penolakan dicatat di dashboard (kolom Status → "ditolak") dan dikabarkan ke Telegram sekali per jenis
alasan per hari. Posisi di-settle otomatis saat market resolve (Up = YES, Down = NO).

---

## 6. Rekomendasi & alert (bukan auto trade)

| Fitur | Aturan | Setting |
|---|---|---|
| Rekomendasi BUY | Jendela 2–1 jam sebelum jam puncak; bracket **likuid** (spread ≤ 10¢) dengan peluang pasar tertinggi; harga = ask; hanya 7 kota top volume | `RECOMMENDATION_MAX_SPREAD`, `TELEGRAM_RECOMMENDATION_TOP_CITIES` |
| Alert Hong Kong | Suhu naik ≥ 0.8°C dalam 30 menit; max hari ini menembus derajat bulat baru; **max mendekati derajat berikutnya** (≥ X.7°C, masih naik/bertahan dekat max, sebelum batas final) dengan harga bracket berikutnya; **risiko posisi** bila bracket YES yang Anda pegang (wallet `/porto` atau paper) mendekati batas atasnya. Masing-masing sekali per derajat per hari; pukul 07–19 HKT; jeda 30 menit antar alert lonjakan; bacaan > 20 menit tidak memicu alert | `HKO_ALERT_*`, `HKO_NEAR_DEGREE_FRACTION`, `HKO_FINAL_HOUR` |
| Waspada berbalik | Bracket favorit hari ini ≥ 90¢ **dan** ada indikasi: suhu terukur dekat titik pindah bracket (HKO ≤ 0.3°C; METAR bulat: bacaan di angka teratas bracket) sambil naik/bertahan sebelum final; bracket sebelah naik ≥ 8¢ dalam 30 menit; data stasiun sudah melewati bracket; atau (HK) prakiraan resmi di atas bracket. Hong Kong + kota top volume, sekali per bracket per hari | `REVERSAL_ALERTS`, `REVERSAL_MIN_PRICE`, `REVERSAL_MOMENTUM` |
| Alert wallet | Transaksi baru wallet yang diikuti, dicek tiap 60 detik, minimal $5 (opsional hanya market cuaca) | `WALLET_POLL_SECONDS`, `WALLET_ALERT_MIN_USDC`, `WALLET_ALERT_WEATHER_ONLY` |
| Rekomendasi wallet | Leaderboard Weather bulan ini; skor = ½ win rate (dihaluskan) + ½ margin PnL/volume; minimal 15 posisi selesai & aktif 7 hari terakhir | `WALLET_DISCOVERY_*` |

---

## 7. Riset & penyesuaian

Setiap evaluasi dicatat di `autotrade_signals` — **ditrade maupun dilewati**, dengan alasan dan konteks
lengkap — lalu hasilnya diisi otomatis setelah market resolve (`app/paper_trading/autotrade_research.py`).

- Laporan: `/autoresearch [hari]` atau dashboard **Riset & saran penyesuaian** (+ export CSV).
- Isi: kalibrasi model, ROI per rentang edge / alasan / menit masuk / kota / sepakat-beda / jam ke puncak,
  fill rate maker.
- Saran ambang: simulasi **satu entri per market** (sampel pertama per market yang lolos ambang), butuh
  ≥ 30 market selesai, mempertahankan ambang sekarang bila hasil seri, menandai strategi yang merugi di
  semua ambang. **Saran tidak diterapkan otomatis** — ubah lewat dashboard.

---

## 8. Keterbatasan yang diketahui

- Backtest belum menunjukkan edge yang jelas: BTC per jam setara dengan pasar; cuaca (bracket favorit)
  hampir impas sebelum biaya, dan market cuaca juga ber-fee.
- Semua ambang 5¢ adalah titik awal, bukan hasil optimasi — gunakan laporan riset untuk menyesuaikan.
- Model cuaca bergantung pada kualitas prakiraan; selisih 1–2°C sudah cukup untuk pindah bracket.
- Fill maker adalah simulasi; di pasar nyata ada antrian dan adverse selection yang bisa lebih buruk.
- Aturan resolusi market bisa berubah — cek deskripsi market bila hasil tampak tidak cocok.
- Suhu bisa naik lagi setelah jam puncak khas. Contoh 30 Sep 2026 (Hong Kong): pasar memberi 33°C 98.5¢
  pukul 14:40 HKT, lalu HKO mencatat 34.2°C sekitar 15:50 — karena itu aturan final HK & alert "mendekati
  derajat berikutnya". Riwayat bacaan HKO per 10 menit: `/hk riwayat [YYYY-MM-DD]` atau `/api/hk/readings.csv`.

## 9. Command Telegram terkait

| Command | Fungsi |
|---|---|
| `/autobot` | Status, pemakaian hari ini, aturan, limit order maker terbuka |
| `/startbot` · `/stopbot` | Jalankan / hentikan auto trader |
| `/autostats [hari]` | Win rate, PnL, ROI total & per strategi |
| `/autoresearch [hari]` | Laporan riset & saran ambang |
| `/tesnotif` | Kirim pesan uji ke chat auto trade (`TELEGRAM_AUTOTRADE_CHAT_ID`) |
