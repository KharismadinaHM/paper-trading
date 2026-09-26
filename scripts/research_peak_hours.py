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

Jalankan ulang tiap pergantian musim:
    python scripts/research_peak_hours.py --days 90
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


def typical_lag(lags, bandwidth: float = 0.5) -> float:
    """Modus yang dihaluskan: pusat jendela ±bandwidth jam dengan jumlah hari terbanyak."""
    median = statistics.median(lags)
    best = max(
        sorted(lags),
        key=lambda c: (sum(1 for x in lags if abs(x - c) <= bandwidth), -abs(c - median)),
    )
    cluster = [x for x in lags if abs(x - best) <= bandwidth]
    return statistics.fmean(cluster)


def analyse(name: str, city, series):
    tz = ZoneInfo(city.tz)
    days = defaultdict(list)
    for ts, temp in series:
        if temp is not None:
            local = ts.astimezone(tz)
            days[local.date()].append((local, temp))

    lag_max, lag_min, max_hours, min_hours = [], [], Counter(), Counter()
    for d, rows in days.items():
        if len(rows) < 23:  # hari tidak lengkap (termasuk hari pergantian DST yang kurang dari 23 jam)
            continue
        t_max = max(rows, key=lambda r: r[1])[0]
        t_min = min(rows, key=lambda r: r[1])[0]
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
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=5)
    start = end - timedelta(days=args.days - 1)
    # Kota yang gagal diambil tetap memakai hasil riset sebelumnya (tidak dihapus dari file)
    previous = json.loads(OUTPUT.read_text()).get("cities", {}) if OUTPUT.exists() else {}
    results = {}
    for name, city in CITIES.items():
        try:
            stats = analyse(name, city, fetch_hourly(city.lat, city.lon, start, end))
        except Exception as err:  # jaringan / API
            print(f"  ! {name}: {err} — memakai hasil sebelumnya" if name in previous else f"  ! {name}: {err}",
                  file=sys.stderr)
            if name in previous:
                results[name] = previous[name]
            continue
        if stats:
            results[name] = stats
            print(f"  {name:<16} max@~{stats['max_hour_mode']:02d}:00 ({stats['max_hour_share']:.0%}) "
                  f"lag {stats['lag_max_hours']:+.2f}h | min@~{stats['min_hour_mode']:02d}:00 "
                  f"({stats['min_hour_share']:.0%}) lag {stats['lag_min_hours']:+.2f}h")
        time.sleep(0.3)  # sopan terhadap API publik

    OUTPUT.write_text(json.dumps({
        "source": "Open-Meteo historical weather API (ERA5 reanalysis), hourly temperature_2m",
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
