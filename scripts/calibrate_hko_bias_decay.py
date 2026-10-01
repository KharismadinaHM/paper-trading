"""
Kalibrasi peluruhan bias proyeksi per jam Hong Kong (/hk jam, HKO_BIAS_DECAY_AT_6H).

Untuk setiap jam (menit :00) dengan bacaan HKO tersimpan sebagai titik awal, proyeksi 1–6 jam ke depan
dihitung ulang seperti hko_hourly.project_ahead dengan beberapa nilai peluruhan, lalu dibandingkan dengan
bacaan HKO real di jam targetnya. Hasil: rata-rata selisih absolut (MAE) per jarak & total, plus nilai
terbaik.

Catatan: model Open-Meteo yang dipakai adalah data per jam saat ini untuk tanggal lampau (hindcast), bukan
prakiraan yang tersedia saat itu, jadi bias model sedikit lebih kecil daripada kondisi live. Gunakan
hasilnya sebagai arah; pilih nilai yang unggul konsisten, bukan selisih tipis.

Jalankan di server (butuh database berisi station_readings):
    python scripts/calibrate_hko_bias_decay.py --days 14
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import get_db_session  # noqa: E402
from app.paper_trading import hko_hourly as hh  # noqa: E402

CANDIDATES = (1.0, 0.75, 0.5, 0.25, 0.0)


def evaluate(readings, model, candidates=CANDIDATES):
    """{decay: {lead: [abs_error, ...]}} dari setiap titik awal :00 yang punya bacaan."""
    hourly = {}
    for ts, temp in readings:
        at = ts.astimezone(hh.HKT)
        if at.minute == 0:
            hourly[at] = temp
    results = {d: {lead: [] for lead in range(1, hh.AHEAD_HOURS + 1)} for d in candidates}
    for origin, temp in hourly.items():
        now = origin + timedelta(minutes=1)
        for decay in candidates:
            for ts, value in hh.project_ahead(now, [(origin, temp)], model, at_6h=decay):
                real = hourly.get(ts)
                if real is not None:
                    lead = round((ts - origin).total_seconds() / 3600)
                    results[decay][lead].append(abs(real - value))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=14, help="jumlah hari terakhir (default 14)")
    args = parser.parse_args()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    db = get_db_session()
    try:
        readings = hh._readings(db, start, end)
    finally:
        db.close()
    if not readings:
        print("Belum ada station_readings HKO pada rentang ini.")
        return
    model = dict(hh.fetch_model_series(start.date() - timedelta(days=1), end.date() + timedelta(days=1)))
    results = evaluate(readings, model)
    leads = list(range(1, hh.AHEAD_HOURS + 1))
    print(f"Bacaan: {len(readings)} · {readings[0][0]:%Y-%m-%d} s.d. {readings[-1][0]:%Y-%m-%d}")
    print("sisa bias @6j | " + " ".join(f"{l}j".rjust(5) for l in leads) + " | total   n")
    best = None
    for decay, by_lead in results.items():
        errors = [e for lead in leads for e in by_lead[lead]]
        if not errors:
            continue
        total = sum(errors) / len(errors)
        cells = " ".join((f"{sum(v) / len(v):.2f}" if v else "  -  ").rjust(5) for v in (by_lead[l] for l in leads))
        print(f"{decay:>12.2f}  | {cells} | {total:.3f} {len(errors):>4}")
        if best is None or total < best[1]:
            best = (decay, total)
    if best:
        print(f"\nTerbaik: HKO_BIAS_DECAY_AT_6H={best[0]} (MAE {best[1]:.3f}°C). Set di .env lalu restart.")


if __name__ == "__main__":
    main()
