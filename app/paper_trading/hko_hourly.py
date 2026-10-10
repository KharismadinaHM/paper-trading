"""
Tabel per jam Hong Kong (HKO): jam · suhu expected · perubahan · suhu real, untuk riwayat satu hari
dan 6 jam ke depan.

- real     = bacaan HKO tersimpan pada menit :00 (toleransi ±10 menit).
- expected = prediksi tersimpan yang dibuat ≥1 jam sebelum jamnya (record_hourly_forecasts, dipanggil
             tiap siklus collector). Untuk jam tanpa prediksi tersimpan (mis. sebelum fitur aktif):
             model Open-Meteo jam itu + bias rata-rata (real − model) 3 jam sebelumnya.
             Untuk jam ke depan: model + bias bacaan terkini, meluruh dengan jarak (HKO_BIAS_DECAY_AT_6H).
- Δ        = perubahan dari jam sebelumnya (real bila ada, selain itu expected).
"""
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.hko_alerts import HKT, STATION, _aware
from app.paper_trading.models import StationForecast, StationReading

logger = get_logger("hko_hourly")

AHEAD_HOURS = 6
MIN_LEAD = timedelta(hours=1)  # prediksi tersimpan dipakai bila dibuat ≥1 jam sebelum jamnya
READING_TOLERANCE = timedelta(minutes=10)
BIAS_HOURS = 3


def fetch_model_series(start: date, end: date) -> List[Tuple[datetime, float]]:
    """[(utc_datetime, temp_c)] Open-Meteo per jam di koordinat HKO untuk rentang tanggal UTC (termasuk lampau)."""
    from app.paper_trading.live_market_data import _cached, _http
    from app.paper_trading.weather_outlook import FORECAST_TTL, FORECAST_URL, HKO_COORDS

    lat, lon = HKO_COORDS

    def load():
        url = (f"{FORECAST_URL}?latitude={lat:.4f}&longitude={lon:.4f}&hourly=temperature_2m&timezone=GMT"
               f"&start_date={start.isoformat()}&end_date={end.isoformat()}")
        hourly = json.loads(_http(url)).get("hourly") or {}
        return [(datetime.fromisoformat(t).replace(tzinfo=timezone.utc), float(v))
                for t, v in zip(hourly.get("time", []), hourly.get("temperature_2m", [])) if v is not None]

    try:
        return _cached(f"hko_model:{start}:{end}", FORECAST_TTL, load)
    except Exception as err:
        logger.warning("Gagal mengambil model Open-Meteo HKO: %s", err)
        return []


def _model_at(model: Dict[datetime, float], ts: datetime) -> Optional[float]:
    """Nilai model pada ts (interpolasi linier antar jam)."""
    base = ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    v0 = model.get(base)
    if v0 is None:
        return None
    frac = (ts - base).total_seconds() / 3600
    v1 = model.get(base + timedelta(hours=1))
    return v0 if not frac or v1 is None else v0 + (v1 - v0) * frac


def _hour_start(ts: datetime) -> datetime:
    return ts.astimezone(HKT).replace(minute=0, second=0, microsecond=0)


def _model_range(first: datetime, last: datetime) -> Dict[datetime, float]:
    start = first.astimezone(timezone.utc).date() - timedelta(days=1)
    end = last.astimezone(timezone.utc).date() + timedelta(days=1)
    return dict(fetch_model_series(start, end))


def _readings(db, start: datetime, end: datetime) -> List[Tuple[datetime, float]]:
    rows = (db.query(StationReading)
            .filter(StationReading.station == STATION, StationReading.observed_at >= start.astimezone(timezone.utc),
                    StationReading.observed_at < end.astimezone(timezone.utc))
            .order_by(StationReading.observed_at).all())
    return [(_aware(r.observed_at), float(r.temp)) for r in rows]


def _reading_near(readings: List[Tuple[datetime, float]], ts: datetime) -> Optional[float]:
    best = min(readings, key=lambda r: abs(r[0] - ts), default=None)
    return best[1] if best and abs(best[0] - ts) <= READING_TOLERANCE else None


def bias_weight(lead_hours: float, at_6h: Optional[float] = None) -> float:
    """Bobot bias pada jarak lead_hours dari bacaan terakhir: 1.0 s.d. 1 jam, linier ke at_6h pada 6 jam."""
    at_6h = settings.HKO_BIAS_DECAY_AT_6H if at_6h is None else at_6h
    if lead_hours <= 1:
        return 1.0
    frac = min((lead_hours - 1) / 5, 1.0)
    return 1.0 + (at_6h - 1.0) * frac


def project_ahead(now: datetime, readings: List[Tuple[datetime, float]], model: Dict[datetime, float],
                  hours: int = AHEAD_HOURS, at_6h: Optional[float] = None) -> List[Tuple[datetime, float]]:
    """
    Prediksi jam-jam berikutnya: model + bias bacaan terkini (real − model saat bacaan itu). Bias meluruh
    dengan jarak (bias_weight): lonjakan sesaat tidak terbawa penuh 6 jam, dan model dipercaya lebih
    banyak untuk jam yang lebih jauh.
    """
    if not readings:
        return []
    latest_at, latest = readings[-1]
    at_obs = _model_at(model, latest_at)
    if at_obs is None:
        return []
    offset = latest - at_obs
    out = []
    first = _hour_start(now) + timedelta(hours=1)
    for i in range(hours):
        ts = first + timedelta(hours=i)
        raw = _model_at(model, ts)
        if raw is not None:
            lead = (ts - latest_at).total_seconds() / 3600
            out.append((ts, round(raw + offset * bias_weight(lead, at_6h), 1)))
    return out


def record_hourly_forecasts(now: Optional[datetime] = None, db=None) -> int:
    """Simpan prediksi 6 jam ke depan, sekali per jam. Mengembalikan jumlah baris baru."""
    now = now or datetime.now(timezone.utc)
    close = db is None
    db = db or get_db_session()
    try:
        bucket = _hour_start(now).astimezone(timezone.utc)
        made = (db.query(StationForecast)
                .filter(StationForecast.station == STATION, StationForecast.made_at >= bucket).first())
        if made is not None:
            return 0
        readings = _readings(db, now - timedelta(hours=3), now + timedelta(minutes=1))
        if not readings:
            return 0
        model = _model_range(now, now + timedelta(hours=AHEAD_HOURS + 1))
        rows = project_ahead(now, readings, model)
        for ts, value in rows:
            db.add(StationForecast(station=STATION, target_at=ts.astimezone(timezone.utc),
                                   made_at=now.astimezone(timezone.utc), value=Decimal(str(value))))
        db.commit()
        return len(rows)
    finally:
        if close:
            db.close()


def run_hourly_forecasts() -> None:
    """Dipanggil dari loop collector; tidak pernah melempar exception."""
    try:
        record_hourly_forecasts()
    except Exception as err:
        logger.error("Gagal menyimpan prediksi per jam HKO: %s", err, exc_info=True)


def hourly_table(day: Optional[date] = None, now: Optional[datetime] = None, db=None) -> Dict[str, Any]:
    """
    Baris per jam untuk satu hari HKT (default hari ini). Hari ini: jam 00:00 s.d. sekarang + 6 jam ke
    depan (boleh lewat tengah malam). Hari lampau: 24 jam penuh.
    Tiap baris: {at, expected, source ('prediksi'|'model'|'proyeksi'), real, delta, error, future}.
    """
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(HKT).date()
    day = day or today
    start = datetime.combine(day, datetime.min.time(), tzinfo=HKT)
    is_today = day == today
    last_past = _hour_start(now) if is_today else start + timedelta(hours=23)
    if day > today:
        last_past = start - timedelta(hours=1)
    close = db is None
    db = db or get_db_session()
    try:
        end = now + timedelta(minutes=1) if is_today else last_past + READING_TOLERANCE + timedelta(minutes=1)
        readings = _readings(db, start - timedelta(hours=BIAS_HOURS + 1), end)
        stored = (db.query(StationForecast)
                  .filter(StationForecast.station == STATION,
                          StationForecast.target_at >= start.astimezone(timezone.utc),
                          StationForecast.target_at <= (last_past + timedelta(hours=AHEAD_HOURS)).astimezone(timezone.utc))
                  .all())
    finally:
        if close:
            db.close()
    model = _model_range(start - timedelta(hours=BIAS_HOURS + 1), last_past + timedelta(hours=AHEAD_HOURS + 1))
    best: Dict[datetime, Tuple[datetime, float]] = {}
    for f in stored:
        target, made = _aware(f.target_at), _aware(f.made_at)
        if target - made >= MIN_LEAD and (target not in best or made > best[target][0]):
            best[target] = (made, float(f.value))

    real_by_hour: Dict[datetime, float] = {}
    rows: List[Dict[str, Any]] = []
    ts = start
    while ts <= last_past:
        real = _reading_near(readings, ts)
        if real is not None:
            real_by_hour[ts] = real
        utc = ts.astimezone(timezone.utc)
        if utc in best:
            expected, source = best[utc][1], "prediksi"
        else:
            raw = _model_at(model, ts)
            biases = [real_by_hour.get(ts - timedelta(hours=k), _reading_near(readings, ts - timedelta(hours=k)))
                      for k in range(1, BIAS_HOURS + 1)]
            diffs = [b - m for k, b in enumerate(biases, 1)
                     if b is not None and (m := _model_at(model, ts - timedelta(hours=k))) is not None]
            expected = round(raw + sum(diffs) / len(diffs), 1) if raw is not None and diffs else (
                round(raw, 1) if raw is not None else None)
            source = "model"
        rows.append({"at": ts, "expected": expected, "source": source, "real": real, "future": False})
        ts += timedelta(hours=1)
    if is_today:
        for at, value in project_ahead(now, readings, model):
            rows.append({"at": at, "expected": value, "source": "proyeksi", "real": None, "future": True})

    prev = None
    for r in rows:
        current = r["real"] if r["real"] is not None else r["expected"]
        r["delta"] = round(current - prev, 1) if current is not None and prev is not None else None
        r["error"] = round(r["real"] - r["expected"], 1) if r["real"] is not None and r["expected"] is not None else None
        if current is not None:
            prev = current
    errors = [abs(r["error"]) for r in rows if r["error"] is not None]
    reals = [r["real"] for r in rows if r["real"] is not None]
    ahead = [r["expected"] for r in rows if r["future"] and r["expected"] is not None]
    return {
        "date": day, "rows": rows,
        "mae": round(sum(errors) / len(errors), 2) if errors else None,
        "max": max(reals, default=None), "min": min(reals, default=None),
        "ahead_max": max(ahead, default=None), "ahead_min": min(ahead, default=None),
    }


def _fmt(v: Optional[float], sign: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v:+.1f}" if sign else f"{v:.1f}"


def format_hourly(day: Optional[date] = None, now: Optional[datetime] = None) -> str:
    """/hk jam: tabel per jam (riwayat hari itu + 6 jam ke depan untuk hari ini)."""
    data = hourly_table(day, now)
    label = data["date"].strftime("%d %b %Y")
    rows = data["rows"]
    if not rows or not any(r["real"] is not None or r["expected"] is not None for r in rows):
        return f"🇭🇰 Belum ada data per jam HKO untuk {label}."
    lines = [f"🇭🇰 *Tabel per jam HKO* {label} (HKT)", "`jam    exp    Δ     real`"]
    divider = False
    for r in rows:
        if r["future"] and not divider:
            lines.append("`── 6 jam ke depan ──────`")
            divider = True
        mark = "*" if r["source"] == "model" and not r["future"] else " "
        day_tag = "+1" if r["at"].date() != data["date"] else "  "
        lines.append(f"`{r['at']:%H:%M}{day_tag}{_fmt(r['expected']):>4}{mark} {_fmt(r['delta'], True):>4}  "
                     f"{_fmt(r['real']):>4}`")
    lines.append("")
    if data["max"] is not None:
        lines.append(f"Real (per jam): max {data['max']:.1f}°C · min {data['min']:.1f}°C")
    if data["ahead_min"] is not None:
        lines.append(f"6 jam ke depan: {data['ahead_min']:.1f}–{data['ahead_max']:.1f}°C")
    if data["mae"] is not None:
        lines.append(f"Akurasi expected: rata-rata meleset {data['mae']:.1f}°C")
    lines.append("exp = prediksi dibuat ≥1 jam sebelumnya · \\* = rekonstruksi model (belum ada prediksi tersimpan) · "
                 "Δ = perubahan dari jam sebelumnya · +1 = hari berikutnya")
    lines.append("Bacaan per jam (menit :00); max/min resmi bisa terjadi di antara jam — lihat `/hk riwayat`.")
    return "\n".join(lines)


def hourly_json(day: Optional[date] = None) -> Dict[str, Any]:
    data = hourly_table(day)
    return {**data, "date": data["date"].isoformat(),
            "rows": [{**r, "at": r["at"].isoformat(), "hour": r["at"].strftime("%H:%M"),
                      "next_day": r["at"].date() != data["date"]} for r in data["rows"]]}
