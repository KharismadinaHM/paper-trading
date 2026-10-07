"""
Kalibrasi bobot model crypto (AUTOTRADE_MODEL_WEIGHT) dari sinyal auto trader yang sudah resolve.

Peluang yang dipakai bot = harga pasar + w × (model − pasar). Script ini:
1. membandingkan akurasi (log-loss) model murni, harga pasar, dan campuran w = 0…1;
2. mencari w terbaik di paruh awal data dan mengujinya di paruh akhir (out-of-sample);
3. mensimulasikan trade (satu entri per market, harga + slippage + fee) untuk beberapa w, ambang edge,
   dan harga minimum.

PENTING: sinyal sebelum perbaikan harga segar (order book di-cache hingga 60 detik) memakai harga Polymarket
yang sudah basi sehingga edge terlihat lebih besar dari kenyataan. Pakai --since dengan waktu deploy perbaikan
itu agar hasilnya bisa dipercaya.

    python scripts/calibrate_crypto_model.py --since 2026-10-08
"""
import argparse
import collections
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import get_db_session  # noqa: E402
from app.paper_trading.models import AutotradeSignal  # noqa: E402

SERIES = ("btc", "btc15", "btc5", "eth", "eth15")


def load(since: datetime):
    db = get_db_session()
    try:
        rows = (db.query(AutotradeSignal).filter(AutotradeSignal.strategy.in_(SERIES),
                                                 AutotradeSignal.outcome.in_(("WIN", "LOSS")),
                                                 AutotradeSignal.created_at >= since)
                .order_by(AutotradeSignal.created_at).all())
    finally:
        db.close()
    out = []
    for r in rows:
        f = json.loads(r.features or "{}")
        price, fee = float(r.price or 0), float(r.fee or 0)
        raw = f.get("model_raw", float(r.model_prob or 0))  # sinyal baru menyimpan model murni di features
        q = min(max(price - float(f.get("slippage") or 0), 0.01), 0.99)
        out.append({"s": r.strategy, "mk": (r.strategy, r.market_id), "t": r.created_at, "m": min(max(raw, 1e-4), 1 - 1e-4),
                    "q": q, "price": price, "fee": fee, "win": r.outcome == "WIN", "pnl": float(r.pnl_per_share or 0)})
    counts = collections.Counter(r["mk"] for r in out)
    for r in out:
        r["w"] = 1 / counts[r["mk"]]  # bobot per market: sampel berurutan di market yang sama saling berkorelasi
    return out


def logloss(rows, w):
    total = weight = 0.0
    for r in rows:
        p = min(max(r["q"] + w * (r["m"] - r["q"]), 1e-4), 1 - 1e-4)
        total += r["w"] * -(math.log(p) if r["win"] else math.log(1 - p))
        weight += r["w"]
    return total / weight if weight else float("nan")


def simulate(rows, w, edge, pmin, pmax=0.9):
    seen, cost, pnl, n, wins = set(), 0.0, 0.0, 0, 0
    for r in rows:
        if r["mk"] in seen:
            continue
        p = r["q"] + w * (r["m"] - r["q"])
        if pmin <= r["price"] <= pmax and p - (r["price"] + r["fee"]) >= edge:
            seen.add(r["mk"])
            n, wins, cost, pnl = n + 1, wins + r["win"], cost + r["price"] + r["fee"], pnl + r["pnl"]
    return n, wins, (pnl / cost if cost else float("nan"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", default="2026-09-01", help="hanya sinyal sejak tanggal ini (YYYY-MM-DD, UTC)")
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    rows = load(since)
    if len(rows) < 200:
        print(f"Baru {len(rows)} sinyal resolve sejak {args.since} — tunggu data lebih banyak (≥ 200).")
        return
    half = len(rows) // 2
    train, test = rows[:half], rows[half:]
    print(f"Sinyal {len(rows)} · market {len({r['mk'] for r in rows})} · sejak {args.since}")
    print("\nLog-loss (lebih kecil = lebih akurat)   train    test")
    for w in (0.0, 0.1, 0.2, 0.3, 0.5, 1.0):
        label = "pasar" if w == 0 else ("model murni" if w == 1 else f"w = {w:.1f}")
        print(f"   {label:<34} {logloss(train, w):.4f}  {logloss(test, w):.4f}")
    best = min(range(0, 101), key=lambda k: logloss(train, k / 100)) / 100
    print(f"\nw terbaik di train: {best:.2f} (test log-loss {logloss(test, best):.4f})")
    print("\nSimulasi di data test (satu entri per market, harga + slippage + fee)")
    print("   w     edge  harga min    n    WR     ROI")
    for w in sorted({1.0, best, 0.3, 0.2}):
        for edge in (0.02, 0.05):
            for pmin in (0.3, 0.5):
                n, wins, roi = simulate(test, w, edge, pmin)
                if n >= 10:
                    print(f"   {w:.2f}  {edge:.2f}  {pmin:.1f}       {n:5} {wins / n:5.0%} {roi:+7.1%}")
    print("\nSet bobot di dashboard → Auto Bot → Ubah aturan & batas → 'Bobot model vs harga pasar crypto'.")


if __name__ == "__main__":
    main()
