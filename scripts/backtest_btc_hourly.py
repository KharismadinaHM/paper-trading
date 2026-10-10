"""
Backtest market Polymarket "Bitcoin Up or Down" per jam.

Resolusi: candle 1H BTC/USDT Binance yang dimulai pada jam di judul (ET); Up jika close ≥ open.

Data (publik): event & hasil (Gamma, slug bitcoin-up-or-down-<bulan>-<tgl>-<tahun>-<jam><am|pm>-et),
riwayat harga Up per menit (CLOB /prices-history), candle 1 menit BTCUSDT (Binance).

Model peluang di menit ke-m setelah open (tanpa drift):
    P(Up) = Φ( ln(S_m / Open) / (σ_1m · √(60 − m)) ),  σ_1m = std log-return 1 menit 120 menit terakhir.
Harga beli = harga Up/Down historis + setengah spread (default 1¢), plus biaya taker Polymarket
crypto: fee = 0.07 · p · (1 − p) per share.

Strategi (satu entri per jam per konfigurasi):
- FAV: beli sisi favorit pasar.
- MODEL≥e: beli sisi dengan edge = P_model − (ask + fee) ≥ e.
ROI/share = total pembayaran / total biaya − 1.

    python scripts/backtest_btc_hourly.py --days 14
"""
import argparse
import json
import math
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BINANCE = "https://data-api.binance.vision"  # mirror publik api.binance.com
HEADERS = {"User-Agent": "Mozilla/5.0 (paper-trading backtest)", "Accept": "application/json"}
ET = ZoneInfo("America/New_York")
CHECKPOINTS = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 58]
EDGES = [0.02, 0.05, 0.08]
TAKER_FEE_RATE = 0.07


def fetch(url, attempts=4):
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=40) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(1.5 * attempt)


def cached(cache: Path, name: str, loader):
    path = cache / name
    if path.exists():
        return json.loads(path.read_text())
    data = loader()
    path.write_text(json.dumps(data))
    return data


def hourly_slug(start_utc: datetime) -> str:
    et = start_utc.astimezone(ET)
    hour = et.hour % 12 or 12
    ampm = "am" if et.hour < 12 else "pm"
    return f"bitcoin-up-or-down-{et:%B}-{et.day}-{et.year}-{hour}{ampm}-et".lower()


def load_event(start_utc: datetime, cache: Path):
    slug = hourly_slug(start_utc)
    events = cached(cache, f"ev_{slug}.json", lambda: fetch(f"{GAMMA}/events?slug={slug}"))
    if not events:
        return None
    m = events[0]["markets"][0]
    if not m.get("closed"):
        return None
    outcomes = json.loads(m.get("outcomes") or "[]")
    prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
    tokens = json.loads(m.get("clobTokenIds") or "[]")
    if outcomes[:2] != ["Up", "Down"] or len(prices) < 2 or len(tokens) < 2:
        return None
    if prices[0] >= 0.99:
        winner = "Up"
    elif prices[1] >= 0.99:
        winner = "Down"
    else:
        return None
    return {"slug": slug, "start": start_utc, "up_token": tokens[0], "winner": winner}


def load_klines(start: datetime, end: datetime, cache: Path):
    """{menit_open_ms: (open, close)} candle 1 menit BTCUSDT."""
    def load():
        rows, cursor = [], int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        while cursor < end_ms:
            batch = fetch(f"{BINANCE}/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={cursor}&limit=1000")
            if not batch:
                break
            rows.extend([[b[0], float(b[1]), float(b[4])] for b in batch])
            cursor = batch[-1][0] + 60_000
        return rows
    rows = cached(cache, f"klines_{start:%Y%m%d%H}_{end:%Y%m%d%H}.json", load)
    return {r[0]: (r[1], r[2]) for r in rows}


def load_history(event, cache: Path):
    s = int(event["start"].timestamp())
    def load():
        data = fetch(f"{CLOB}/prices-history?market={event['up_token']}&startTs={s - 7200}&endTs={s + 3700}&fidelity=1")
        return data.get("history", [])
    return sorted(cached(cache, f"ph_{event['slug']}.json", load), key=lambda p: p["t"])


def price_after(history, ts, max_wait=60):
    """Sampel harga pertama SETELAH ts (konservatif: pasar sudah melihat info yang dipakai model)."""
    for p in history:
        if ts <= p["t"] <= ts + max_wait:
            return p["p"]
        if p["t"] > ts + max_wait:
            break
    return None


def price_at(history, ts, max_age=300):
    best = None
    for p in history:
        if p["t"] <= ts:
            best = p
        else:
            break
    return best["p"] if best and ts - best["t"] <= max_age else None


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def fee(p):
    return TAKER_FEE_RATE * p * (1 - p)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--half-spread", type=float, default=0.01)
    parser.add_argument("--timing", choices=("after", "before"), default="after",
                        help="after = harga pasar dari sampel sesudah keputusan model (konservatif, default); "
                             "before = sampel terakhir sebelum (bisa memberi edge palsu, riwayat CLOB per menit)")
    parser.add_argument("--max-age", type=int, default=60,
                        help="Umur maksimum harga pasar historis (detik); harga basi bisa menciptakan edge palsu")
    parser.add_argument("--cache", type=str, default=str(ROOT / ".backtest_cache" / "btc"))
    args = parser.parse_args()
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    starts = [now - timedelta(hours=h) for h in range(2, args.days * 24 + 2)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        events = [e for e in pool.map(lambda s: load_event(s, cache), starts) if e]
    with ThreadPoolExecutor(max_workers=8) as pool:
        histories = list(pool.map(lambda e: load_history(e, cache), events))
    klines = load_klines(min(starts) - timedelta(hours=3), now, cache)
    print(f"{len(events)} jam resolved dari {len(starts)} jam", file=sys.stderr)

    trades = defaultdict(list)        # (strategi, menit) → [(biaya, menang)]
    brier = defaultdict(lambda: [0.0, 0.0, 0])  # menit → [pasar, model, n]
    for event, history in zip(events, histories):
        t0 = int(event["start"].timestamp() * 1000)
        if t0 not in klines:
            continue
        open_price = klines[t0][0]
        up_won = event["winner"] == "Up"
        for m in CHECKPOINTS:
            t_ms = t0 + m * 60_000
            last = klines.get(t_ms - 60_000)  # candle yang berakhir tepat di menit m
            closes = [klines[k][1] for k in range(t_ms - 121 * 60_000, t_ms, 60_000) if k in klines]
            p_up = (price_after(history, t_ms // 1000, max_wait=args.max_age) if args.timing == "after"
                    else price_at(history, t_ms // 1000, max_age=args.max_age))
            if last is None or len(closes) < 60 or p_up is None:
                continue
            rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
            sigma = statistics.pstdev(rets) or 1e-9
            model_up = phi(math.log(last[1] / open_price) / (sigma * math.sqrt(60 - m)))
            b = brier[m]
            b[0] += (p_up - up_won) ** 2
            b[1] += (model_up - up_won) ** 2
            b[2] += 1

            asks = {"Up": min(0.99, p_up + args.half_spread), "Down": min(0.99, 1 - p_up + args.half_spread)}
            probs = {"Up": model_up, "Down": 1 - model_up}
            fav = "Up" if p_up >= 0.5 else "Down"
            cost = asks[fav] + fee(asks[fav])
            trades[("FAV", m)].append((cost, event["winner"] == fav))
            side = max(asks, key=lambda s: probs[s] - asks[s] - fee(asks[s]))
            edge = probs[side] - asks[side] - fee(asks[side])
            for e in EDGES:
                if edge >= e:
                    trades[(f"MODEL≥{e:.2f}", m)].append((asks[side] + fee(asks[side]), event["winner"] == side))

    print(f"\nBTC Up/Down per jam · {len(events)} jam · setengah spread {args.half_spread * 100:.0f}¢ · "
          f"harga pasar {args.timing} (≤{args.max_age} dtk) · fee taker 0.07·p·(1−p)\n")
    print(f"{'menit':>5}  {'Brier pasar':>11} {'Brier model':>11}")
    for m in CHECKPOINTS:
        b = brier[m]
        if b[2]:
            print(f"{m:>5}  {b[0] / b[2]:>11.4f} {b[1] / b[2]:>11.4f}")
    print(f"\n{'strategi':<11} {'menit':>5} {'n':>5} {'win':>6} {'biaya':>6} {'ROI/sh':>7}")
    for strategy in ["FAV"] + [f"MODEL≥{e:.2f}" for e in EDGES]:
        for m in CHECKPOINTS:
            rows = trades.get((strategy, m), [])
            if not rows:
                continue
            wins = sum(1 for _, w in rows if w)
            spent = sum(c for c, _ in rows)
            print(f"{strategy:<11} {m:>5} {len(rows):>5} {wins / len(rows):>6.0%} {spent / len(rows):>6.2f} "
                  f"{wins / spent - 1:>+7.1%}")
        print()


if __name__ == "__main__":
    main()
