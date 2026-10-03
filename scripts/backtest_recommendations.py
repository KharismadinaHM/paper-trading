"""
Backtest saran beli market suhu Polymarket terhadap event yang sudah resolve.

Data (semua publik, tanpa API key):
- Event & hasil resolusi: Gamma API (event weather yang sudah closed).
- Riwayat harga tiap bracket: Polymarket CLOB /prices-history.
- Observasi stasiun resolusi (METAR bandara, kode ICAO dari deskripsi market): arsip IEM ASOS.

Strategi yang diuji pada beberapa waktu masuk relatif terhadap awal jam puncak perkiraan aplikasi:
- FAV: beli YES bracket dengan harga tertinggi (= strategi rekomendasi saat ini).
- OBS: beli YES bracket yang memuat suhu tertinggi/terendah yang SUDAH terukur di stasiun
       sejak tengah malam lokal sampai waktu masuk.
- OBS+: seperti OBS, tetapi hanya jika observasi terakhir sudah ≥1° menjauh dari angka ekstrem
        (suhu sudah turun setelah puncak / naik setelah titik terendah).
ROI/share = membeli 1 share tiap saran: win rate / harga rata-rata − 1 (tahan outlier).
ROI/$1    = membeli $1 tiap saran (menang: (1 − p) / p, kalah: −1); didominasi harga sangat murah.
Harga CLOB adalah harga terakhir/mid, bukan ask, jadi ROI nyata sedikit lebih buruk.

    python scripts/backtest_recommendations.py --days 10
"""
import argparse
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.market_collector.collector import determine_winning_outcome  # noqa: E402
from app.paper_trading.cities import resolve_city  # noqa: E402
from app.paper_trading.weather_peaks import city_timezone, parse_temperature_market, recommendation_window  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
HEADERS = {"User-Agent": "Mozilla/5.0 (paper-trading backtest)", "Accept": "application/json"}
OFFSETS = [-3, -2, -1, 0, 1, 2, 3, 4, 6]  # jam relatif terhadap awal jam puncak perkiraan
BRACKET_RE = re.compile(r"(-?\d+)(?:\s*-\s*(-?\d+))?\s*°\s*([CF])(?:\s+or\s+(below|lower|higher|above))?", re.I)
STATION_RE = re.compile(r"site=([A-Za-z0-9]{4})\b|/([A-Z]{4})\b")


def fetch(url: str, raw: bool = False, attempts: int = 4):
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=60) as resp:
                body = resp.read().decode("utf-8")
            return body if raw else json.loads(body)
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(2 * attempt)


def cached(cache: Path, name: str, loader):
    path = cache / name
    if path.exists():
        return json.loads(path.read_text())
    data = loader()
    path.write_text(json.dumps(data))
    return data


def parse_bracket(label: str):
    """'32°C' → (32, 32, 'C'); '26°C or below' → (-inf, 26, 'C'); '60-61°F' → (60, 61, 'F')."""
    m = BRACKET_RE.search(label or "")
    if not m:
        return None
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    tail = (m.group(4) or "").lower()
    if tail in ("below", "lower"):
        lo = -math.inf
    elif tail in ("higher", "above"):
        hi = math.inf
    return lo, hi, m.group(3).upper()


def contains(bracket, value: float) -> bool:
    lo, hi, _ = bracket
    v = math.floor(value + 0.5)  # pembulatan ke derajat terdekat seperti tabel NOAA
    return lo <= v <= hi


def discover_events(start: date, end: date, cache: Path):
    """Event suhu weather yang sudah closed dengan tanggal lokal dalam [start, end]."""
    def load():
        events, offset = [], 0
        end_min = (start - timedelta(days=1)).isoformat() + "T00:00:00Z"
        while True:
            url = (f"{GAMMA}/events?tag_slug=weather&closed=true&limit=100&offset={offset}"
                   f"&end_date_min={end_min}&order=endDate&ascending=false")
            page = fetch(url)
            if not page:
                break
            events.extend(page)
            offset += 100
            if offset > 5000:
                break
        return events

    out = []
    for ev in cached(cache, f"events_{start}_{end}.json", load):
        parsed = parse_temperature_market(ev.get("title", ""), now=datetime.now(timezone.utc))
        if parsed is None or not (start <= parsed.local_date <= end):
            continue
        city = resolve_city(parsed.city)
        markets, station = [], None
        for m in ev.get("markets", []):
            bracket = parse_bracket(m.get("groupItemTitle", ""))
            tokens = json.loads(m.get("clobTokenIds") or "[]")
            if not bracket or not tokens:
                continue
            text = f"{m.get('resolutionSource') or ''} {m.get('description') or ''}"
            sm = STATION_RE.search(text)
            if sm and station is None:
                station = (sm.group(1) or sm.group(2)).upper()
            markets.append({"label": m["groupItemTitle"], "bracket": bracket, "token": tokens[0],
                            "winner": determine_winning_outcome(m)})
        if markets and any(x["winner"] == "YES" for x in markets):
            out.append({"city": city, "kind": parsed.kind, "date": parsed.local_date, "station": station,
                        "markets": markets})
    return out


def price_history(token: str, day_start: datetime, cache: Path):
    def load():
        start = int((day_start - timedelta(days=1)).timestamp())
        end = int((day_start + timedelta(days=2)).timestamp())
        url = f"{CLOB}/prices-history?market={token}&startTs={start}&endTs={end}&fidelity=10"
        return fetch(url).get("history", [])
    return cached(cache, f"price_{token[-24:]}_{day_start:%Y%m%d}.json", load)


def station_obs(station: str, start: date, end: date, cache: Path):
    """[(utc_ts, tmpc, tmpf)] dari arsip METAR IEM (rutin + special)."""
    def load():
        params = urllib.parse.urlencode({
            "station": station, "data": "tmpc,tmpf", "tz": "Etc/UTC", "format": "onlycomma",
            "latlon": "no", "missing": "M", "trace": "T",
            "year1": start.year, "month1": start.month, "day1": start.day,
            "year2": end.year, "month2": end.month, "day2": end.day,
        }) + "&report_type=3&report_type=4"
        rows = []
        for line in fetch(f"{IEM}?{params}", raw=True).strip().splitlines()[1:]:
            parts = line.split(",")
            if len(parts) < 4 or "M" in (parts[2], parts[3]):
                continue
            ts = datetime.strptime(parts[1], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
            rows.append((ts, float(parts[2]), float(parts[3])))
        return rows
    return cached(cache, f"obs_{station}_{start}_{end}.json", load)


def price_at(history, ts: float, max_age: float = 3 * 3600):
    best = None
    for point in history:
        if point["t"] <= ts:
            best = point
        else:
            break
    if best is None or ts - best["t"] > max_age:
        return None
    return best["p"]


def roi(p: float, won: bool) -> float:
    return (1 - p) / p if won else -1.0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--cache", type=str, default=str(ROOT / ".backtest_cache"))
    parser.add_argument("--max-price", type=float, default=0.97, help="Lewati entri dengan harga di atas ini")
    parser.add_argument("--min-price", type=float, default=0.05, help="Lewati entri dengan harga di bawah ini")
    args = parser.parse_args()

    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    end = datetime.now(timezone.utc).date() - timedelta(days=2)
    start = end - timedelta(days=args.days - 1)
    events = discover_events(start, end, cache)
    print(f"{len(events)} event resolved {start} … {end}", file=sys.stderr)

    # Riwayat harga (paralel) & observasi per stasiun
    jobs = []
    for ev in events:
        tz = city_timezone(ev["city"])
        if tz is None:
            continue
        ev["tz"] = tz
        day_start = datetime.combine(ev["date"], datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
        ev["day_start"], ev["day_end"] = day_start, day_start + timedelta(days=1)
        jobs += [(m, day_start) for m in ev["markets"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for (m, _), hist in zip(jobs, pool.map(lambda j: price_history(j[0]["token"], j[1], cache), jobs)):
            m["history"] = sorted(hist, key=lambda p: p["t"])
    obs = {}
    for station in sorted({ev["station"] for ev in events if ev.get("station")}):
        try:
            obs[station] = station_obs(station, start - timedelta(days=1), end + timedelta(days=2), cache)
        except Exception as err:
            print(f"  ! observasi {station}: {err}", file=sys.stderr)

    stats = defaultdict(list)       # (strategi, jenis, offset) → [(harga, menang)]
    peak_errors = defaultdict(list)  # jenis → selisih jam puncak aktual stasiun − perkiraan (jam)
    for ev in events:
        if "tz" not in ev:
            continue
        window = recommendation_window(ev["city"], ev["kind"], ev["date"])
        if window is None:
            continue
        peak = window.peak_start.astimezone(timezone.utc)
        station_rows = [r for r in obs.get(ev.get("station") or "", [])
                        if ev["day_start"].timestamp() <= r[0] < ev["day_end"].timestamp()]
        unit = ev["markets"][0]["bracket"][2]
        col = 1 if unit == "C" else 2
        if station_rows and ev["city"] != "Hong Kong":  # HK memakai HKO, bukan METAR
            pick = max if ev["kind"] == "highest" else min
            extreme = pick(r[col] for r in station_rows)
            tied = [r[0] for r in station_rows if r[col] == extreme]
            actual_ts = (min(tied) + max(tied)) / 2  # titik tengah plateau bacaan bulat, bukan yang pertama
            peak_errors[ev["kind"]].append((actual_ts - window.peak_start.timestamp()) / 3600)
        for offset in OFFSETS:
            ts = (peak + timedelta(hours=offset)).timestamp()
            priced = [(price_at(m["history"], ts), m) for m in ev["markets"]]
            priced = [(p, m) for p, m in priced if p is not None]
            if priced:
                p, m = max(priced, key=lambda x: x[0])
                if args.min_price <= p <= args.max_price:
                    stats[("FAV", ev["kind"], offset)].append((p, m["winner"] == "YES"))
            if station_rows and ev["city"] != "Hong Kong":
                seen = [r[col] for r in station_rows if r[0] <= ts]
                if seen:
                    extreme = max(seen) if ev["kind"] == "highest" else min(seen)
                    target = next((m for m in ev["markets"] if contains(m["bracket"], extreme)), None)
                    p = price_at(target["history"], ts) if target else None
                    if p is not None and args.min_price <= p <= args.max_price:
                        won = target["winner"] == "YES"
                        stats[("OBS", ev["kind"], offset)].append((p, won))
                        moved_away = abs(seen[-1] - extreme) >= (1 if unit == "C" else 2)
                        if moved_away:
                            stats[("OBS+", ev["kind"], offset)].append((p, won))

    print(f"\nBacktest {start} … {end} · {len(events)} event · harga maks {args.max_price}")
    print("offset = jam relatif awal jam puncak perkiraan (−2 = awal jendela rekomendasi saat ini)\n")
    print(f"{'strategi':<5} {'jenis':<8} {'offset':>6} {'n':>5} {'win':>6} {'harga':>6} {'ROI/sh':>7} {'ROI/$1':>7}")
    for strategy in ("FAV", "OBS", "OBS+"):
        for kind in ("highest", "lowest"):
            for offset in OFFSETS:
                rows = stats.get((strategy, kind, offset), [])
                if not rows:
                    continue
                wins = sum(1 for _, w in rows if w)
                avg_price = sum(p for p, _ in rows) / len(rows)
                print(f"{strategy:<5} {kind:<8} {offset:>+6} {len(rows):>5} {wins / len(rows):>6.0%} "
                      f"{avg_price:>6.2f} {wins / len(rows) / avg_price - 1:>+7.0%} "
                      f"{sum(roi(p, w) for p, w in rows) / len(rows):>+7.0%}")
            print()
    for kind, errs in peak_errors.items():
        errs.sort()
        med = errs[len(errs) // 2]
        within = sum(1 for e in errs if -1 <= e <= 2) / len(errs)
        print(f"Jam puncak {kind}: median selisih aktual − awal puncak perkiraan {med:+.1f} jam, "
              f"{within:.0%} dalam [−1, +2] jam (n={len(errs)})")


if __name__ == "__main__":
    main()
