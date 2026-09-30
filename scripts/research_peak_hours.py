"""
Riset jam puncak suhu harian per kota dari data historis per jam (Open-Meteo / ERA5).

Untuk setiap kota dan setiap hari (hari kalender lokal, sama seperti resolusi Polymarket):
- lag_max = waktu suhu tertinggi  - solar noon   (jam)
- lag_min = waktu suhu terendah   - matahari terbit (jam)
Yang dipakai aplikasi adalah lag "tipikal" = modus yang dihaluskan (titik dengan kepadatan hari
terbanyak dalam jendela ±30 menit, lalu dirata-rata). Median juga disimpan untuk pembanding;
median cenderung tertarik ke pagi oleh hari hujan/berawan (puncak sebelum siang), sedangkan
modus mewakili hari normal. Aplikasi menghitung jam puncak untuk tanggal apa pun = solar noon /
sunrise tanggal itu + lag, sehingga pergeseran musim & DST ikut terhitung.

Sumber data:
- --source station (disarankan): observasi METAR di STASIUN RESOLUSI tiap market (bandara, kode ICAO
  dari deskripsi market Polymarket; arsip IEM ASOS). Inilah angka yang dipakai untuk resolusi.
- --source grid: Open-Meteo ERA5 di koordinat pusat kota (cara lama). Hong Kong (HKO, tidak ada di
  arsip METAR) dan kota yang gagal diambil tetap memakai hasil sebelumnya.

Jalankan ulang tiap pergantian musim:
    python scripts/research_peak_hours.py --source station --days 60
"""
import argparse
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.paper_trading.cities import CITIES  # noqa: E402
from app.paper_trading.solar import solar_noon_utc, sunrise_utc  # noqa: E402

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
GAMMA_URL = "https://gamma-api.polymarket.com/events"
HEADERS = {"User-Agent": "Mozilla/5.0 (paper-trading peak research)"}
OUTPUT = ROOT / "app" / "paper_trading" / "peak_calibration.json"


def fetch_hourly(lat: float, lon: float, start: date, end: date, attempts: int = 3):
    params = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": "temperature_2m", "timezone": "GMT",
    })
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(f"{ARCHIVE_URL}?{params}", timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(2 * attempt)
    times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in data["hourly"]["time"]]
    return list(zip(times, data["hourly"]["temperature_2m"]))


def fetch_station(station: str, start: date, end: date, chunk_days: int = 10):
    """[(utc_datetime, tmpc)] METAR dari arsip IEM, diambil per potongan (IEM memutus request besar)."""
    rows, cursor = [], start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        rows.extend(_fetch_station_chunk(station, cursor, chunk_end + timedelta(days=1)))  # tumpang tindih 1 hari
        cursor = chunk_end + timedelta(days=1)
        time.sleep(2)
    return sorted(set(rows))


def _fetch_station_chunk(station: str, start: date, end: date, attempts: int = 6):
    """Satu potongan observasi METAR (rutin + special) dari arsip IEM."""
    params = urllib.parse.urlencode({
        "station": station, "data": "tmpc", "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no",
        "missing": "M", "trace": "T", "year1": start.year, "month1": start.month, "day1": start.day,
        "year2": end.year, "month2": end.month, "day2": end.day,
    }) + "&report_type=3&report_type=4"
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(f"{IEM_URL}?{params}", headers=HEADERS)
            with urllib.request.urlopen(req, timeout=120) as resp:
                text = resp.read().decode("utf-8")
            break
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(10 * attempt)  # arsip IEM membatasi request beruntun
    rows = []
    for line in text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 3 and parts[2] not in ("M", ""):
            ts = datetime.strptime(parts[1], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            rows.append((ts, float(parts[2])))
    return rows


def resolution_stations():
    """{kota: ICAO} dari market suhu Polymarket yang sedang buka (stasiun resolusi)."""
    from app.market_collector.collector import parse_resolution_station
    from app.paper_trading.cities import resolve_city
    from app.paper_trading.weather_peaks import parse_temperature_market

    stations = {}
    for offset in range(0, 2000, 100):
        url = f"{GAMMA_URL}?tag_slug=weather&closed=false&limit=100&offset={offset}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=60) as resp:
            events = json.loads(resp.read().decode("utf-8"))
        if not events:
            break
        for ev in events:
            parsed = parse_temperature_market(ev.get("title", ""))
            markets = ev.get("markets") or []
            if parsed and markets:
                station = parse_resolution_station(markets[0])
                if station and station != "HKO":
                    stations.setdefault(resolve_city(parsed.city), station)
    return stations


def typical_lag(lags, bandwidth: float = 0.5) -> float:
    """Modus yang dihaluskan: pusat jendela ±bandwidth jam dengan jumlah hari terbanyak."""
    median = statistics.median(lags)
    best = max(
        sorted(lags),
        key=lambda c: (sum(1 for x in lags if abs(x - c) <= bandwidth), -abs(c - median)),
    )
    cluster = [x for x in lags if abs(x - best) <= bandwidth]
    return statistics.fmean(cluster)


def _extreme_time(rows, pick):
    """
    Waktu suhu ekstrem hari itu. Bacaan METAR bulat sering membuat nilai ekstrem bertahan beberapa jam
    (mis. 25°C dari 01:00 s/d 06:00); ambil titik tengah semua bacaan bernilai ekstrem, bukan yang
    pertama (yang membuat jam ekstrem bergeser terlalu awal).
    """
    value = pick(r[1] for r in rows)
    times = sorted(r[0] for r in rows if r[1] == value)
    return times[0] + (times[-1] - times[0]) / 2 if len(times) > 1 else times[0]


def analyse(name: str, city, series, min_rows: int = 23):
    tz = ZoneInfo(city.tz)
    days = defaultdict(list)
    for ts, temp in series:
        if temp is not None:
            local = ts.astimezone(tz)
            days[local.date()].append((local, temp))

    lag_max, lag_min, max_hours, min_hours = [], [], Counter(), Counter()
    for d, rows in days.items():
        if len(rows) < min_rows:  # hari tidak lengkap (termasuk hari pergantian DST yang kurang dari 23 jam)
            continue
        hours_covered = {r[0].hour for r in rows}
        if len(hours_covered) < 20:  # data stasiun berlubang di hari itu
            continue
        t_max = _extreme_time(rows, max)
        t_min = _extreme_time(rows, min)
        noon = solar_noon_utc(d, city.lon)
        rise = sunrise_utc(d, city.lat, city.lon)
        lag_max.append((t_max - noon).total_seconds() / 3600)
        if rise is not None:
            lag_min.append((t_min - rise).total_seconds() / 3600)
        max_hours[t_max.hour] += 1
        min_hours[t_min.hour] += 1

    if not lag_max:
        return None
    return {
        "days": len(lag_max),
        "lag_max_hours": round(typical_lag(lag_max), 2),
        "lag_min_hours": round(typical_lag(lag_min), 2) if lag_min else None,
        "lag_max_median_hours": round(statistics.median(lag_max), 2),
        "lag_min_median_hours": round(statistics.median(lag_min), 2) if lag_min else None,
        "max_hour_mode": max_hours.most_common(1)[0][0],
        "min_hour_mode": min_hours.most_common(1)[0][0],
        "max_hour_share": round(max_hours.most_common(1)[0][1] / len(lag_max), 2),
        "min_hour_share": round(min_hours.most_common(1)[0][1] / len(lag_max), 2),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=90, help="Jumlah hari data historis (default 90)")
    parser.add_argument("--end", type=str, default=None, help="Tanggal akhir YYYY-MM-DD (default: 5 hari lalu)")
    parser.add_argument("--only-missing", action="store_true",
                        help="Lewati kota yang sudah punya hasil dari stasiun (lanjutkan riset yang terputus)")
    parser.add_argument("--source", choices=("station", "grid"), default="station",
                        help="station = METAR stasiun resolusi market (default), grid = Open-Meteo pusat kota")
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=5)
    start = end - timedelta(days=args.days - 1)
    # Kota yang gagal diambil tetap memakai hasil riset sebelumnya (tidak dihapus dari file)
    previous = json.loads(OUTPUT.read_text()).get("cities", {}) if OUTPUT.exists() else {}
    results = {}
    stations = resolution_stations() if args.source == "station" else {}
    if args.source == "station":
        print(f"{len(stations)} stasiun resolusi ditemukan", file=sys.stderr)
    for name, city in CITIES.items():
        station = stations.get(name)
        if args.only_missing and str(previous.get(name, {}).get("source", "")).startswith("station"):
            results[name] = previous[name]
            continue
        if args.source == "station" and not station:
            if name in previous:
                results[name] = previous[name]  # mis. Hong Kong (HKO) atau kota tanpa market aktif
            continue
        try:
            series = (fetch_station(station, start, end + timedelta(days=1)) if station
                      else fetch_hourly(city.lat, city.lon, start, end))
            stats = analyse(name, city, series)
            if stats and station:
                stats["source"] = f"station:{station}"
        except Exception as err:  # jaringan / API
            print(f"  ! {name}: {err} — memakai hasil sebelumnya" if name in previous else f"  ! {name}: {err}",
                  file=sys.stderr)
            if name in previous:
                results[name] = previous[name]
            continue
        if stats and name in previous:
            # Penjaga plausibilitas: data stasiun berlubang (mis. bacaan malam hilang) bisa memberi jam
            # ekstrem yang mustahil — pakai hasil sebelumnya untuk metrik itu saja.
            for metric, lo, hi in (("max", -2.0, 5.0), ("min", -4.0, 2.0)):
                lag = stats.get(f"lag_{metric}_hours")
                if lag is not None and not lo <= lag <= hi:
                    print(f"  ! {name}: lag_{metric} {lag:+.2f} jam tidak wajar — memakai hasil sebelumnya",
                          file=sys.stderr)
                    for key in (f"lag_{metric}_hours", f"lag_{metric}_median_hours", f"{metric}_hour_mode",
                                f"{metric}_hour_share"):
                        if key in previous[name]:
                            stats[key] = previous[name][key]
                    stats[f"{metric}_source"] = previous[name].get("source", "grid")
        if not stats and name in previous:
            results[name] = previous[name]  # data stasiun terlalu berlubang untuk dianalisis
            print(f"  ! {name}: data tidak cukup — memakai hasil sebelumnya", file=sys.stderr)
            continue
        if stats:
            results[name] = stats
            print(f"  {name:<16} max@~{stats['max_hour_mode']:02d}:00 ({stats['max_hour_share']:.0%}) "
                  f"lag {stats['lag_max_hours']:+.2f}h | min@~{stats['min_hour_mode']:02d}:00 "
                  f"({stats['min_hour_share']:.0%}) lag {stats['lag_min_hours']:+.2f}h")
        time.sleep(4.0 if station else 0.3)  # sopan terhadap API publik (IEM membatasi request beruntun)

    OUTPUT.write_text(json.dumps({
        "source": ("METAR observations at each market's resolution station (IEM ASOS archive); cities without an "
                   "ICAO station (e.g. Hong Kong/HKO) keep earlier Open-Meteo ERA5 results"
                   if args.source == "station" else
                   "Open-Meteo historical weather API (ERA5 reanalysis), hourly temperature_2m"),
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": (
            "lag_*_hours = smoothed mode (densest ±30 min cluster, averaged) of (time of daily max - solar noon) "
            "and (time of daily min - sunrise); lag_*_median_hours = medians; local calendar day"
        ),
        "cities": results,
    }, indent=2, ensure_ascii=False) + "\n")
    print(f"\n{len(results)}/{len(CITIES)} kota disimpan ke {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
