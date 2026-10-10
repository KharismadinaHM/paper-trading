"""
Simulasi Monte Carlo suhu max/min Hong Kong (HKO) — gabungan banyak sumber, bukan satu model saja.

Satu simulasi = satu jalur suhu per jam untuk sisa hari (atau besok), dibangun dari:
1. **Skenario fisika**: anggota ensemble Open-Meteo (ECMWF ENS 51, GFS ENS 31, ICON EPS 40 — suhu & hujan per jam yang
   konsisten secara fisika) + 5 model deterministik (ECMWF, GFS, ICON, JMA, CMA). Tiap jalur dikoreksi bacaan HKO
   terakhir (selisih bacaan − jalur saat itu, meluruh dengan jarak seperti proyeksi per jam).
2. **Error historis**: tiap jalur diberi beberapa realisasi noise AR(1) per jam dengan besaran dari error proyeksi
   HKO yang terukur (hk_bot.sigma_for), sehingga ketidakpastian lokal stasiun ikut terwakili.
3. **Cuaca saat ini**: bila nowcast radar / peringatan HKO menunjukkan hujan ±2 jam, sebagian simulasi (yang jalurnya
   sendiri belum hujan) mendapat kejutan pendinginan 1.5–3.5°C yang memudar perlahan (hujan bisa menjatuhkan suhu
   3–5°C, dan puncak sesudahnya ikut tertahan).
4. **Prakiraan resmi HKO**: puncak/lembah simulasi digeser sebagian ke angka resmi HKO (bobot HK_OFFICIAL_WEIGHT).
5. **Kalibrasi**: koreksi bias historis per jam & rezim cuaca (hk_calibration).
6. **Data historis (klimatologi)**: sebagian kecil sampel diambil dari max/min harian 30 tahun di tanggal ±3 hari,
   digeser anomali hari-hari terakhir (persistensi). Bobotnya mengecil saat puncak mendekat (data hari ini lebih kuat).

Hasil akhir per simulasi = max(terukur, puncak jalur) / min(terukur, lembah jalur) → peluang per bracket = proporsi
simulasi yang jatuh di bracket itu ("X°C" = X.0–X.9).
"""
import hashlib
import json
import math
import random
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.logging import get_logger
from app.paper_trading.hko_alerts import HKT

logger = get_logger("hk_simulation")

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ENSEMBLE_MODELS = "ecmwf_ifs025,gfs025,icon_seamless"
SIMS_PER_PATH = 8
NOISE_RHO = 0.8            # korelasi noise antar jam (AR(1))
NOISE_SCALE = 0.6          # sebagian ketidakpastian sudah diwakili sebaran ensemble
RAIN_SHOCK_PROB = 0.7      # peluang simulasi mendapat kejutan hujan bila hujan diperkirakan
RAIN_SHOCK = (1.5, 3.5)    # °C
RAIN_RECOVERY_H = 3.0      # pendinginan hujan memudar eksponensial (±separuh hilang dalam 2 jam)
CLIM_WEIGHT_MAX = 0.15     # porsi sampel klimatologi paling besar (jauh sebelum puncak / besok)
CLIM_HORIZON_H = 8.0       # porsi klimatologi menyusut linier sampai 0 saat puncak tinggal 0 jam
ANOMALY_SHRINK = 0.5
_clim_cache: Dict[Tuple[str, str], Tuple[float, List[float]]] = {}


# --- Sumber data -------------------------------------------------------------------------

def ensemble_members() -> List[Dict[str, Any]]:
    """[{name, series: [(utc, temp)], precip: {utc: mm}}] anggota ensemble Open-Meteo (cache 1 jam)."""
    from app.paper_trading.live_market_data import _cached, _http
    from app.paper_trading.weather_outlook import HKO_COORDS

    lat, lon = HKO_COORDS

    def load():
        url = (f"{ENSEMBLE_URL}?latitude={lat:.4f}&longitude={lon:.4f}&hourly=temperature_2m,precipitation"
               f"&models={ENSEMBLE_MODELS}&timezone=GMT&forecast_days=3")
        h = json.loads(_http(url, timeout=30)).get("hourly") or {}
        times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in h.get("time", [])]
        out = []
        for key, values in h.items():
            if not key.startswith("temperature_2m") or "member" not in key:
                continue  # rata-rata/kontrol diwakili anggota & model deterministik
            series = [(t, float(v)) for t, v in zip(times, values) if v is not None]
            if len(series) < 24:
                continue
            precip_key = key.replace("temperature_2m", "precipitation", 1)
            precip = {t: float(v) for t, v in zip(times, h.get(precip_key) or []) if v is not None}
            out.append({"name": key.replace("temperature_2m_", ""), "series": series, "precip": precip})
        return out

    try:
        return _cached("hk_ensemble_members", 3600, load)
    except Exception as err:
        logger.warning("Gagal mengambil anggota ensemble Open-Meteo: %s", err)
        return []


def all_paths() -> List[Dict[str, Any]]:
    """Anggota ensemble + model deterministik (tanpa data hujan)."""
    from app.paper_trading.hk_forecast import ensemble_series

    paths = list(ensemble_members())
    for model, series in ensemble_series().items():
        paths.append({"name": model, "series": series, "precip": {}})
    return paths


def climatology(day: date, kind: str) -> List[float]:
    """Max/min harian HKO 30 tahun di tanggal ±3 hari, digeser anomali 5 hari terakhir (dikecilkan). Cache 6 jam."""
    from app.paper_trading import hk_climate as hc

    key = (day.isoformat(), kind)
    hit = _clim_cache.get(key)
    if hit and time.monotonic() - hit[0] < 6 * 3600:
        return hit[1]
    try:
        series, _, _ = hc.daily_series(day)
        s = series["max" if kind == "highest" else "min"]
        years = list(range(day.year - hc.DIST_YEARS, day.year))
        values = hc._window(s, day.month, day.day, years)
        anomalies = []
        for k in range(1, 6):
            d = day - timedelta(days=k)
            if d in s:
                normal = hc._window(s, d.month, d.day, years)
                if normal:
                    anomalies.append(s[d] - statistics.median(normal))
        shift = ANOMALY_SHRINK * (sum(anomalies) / len(anomalies)) if anomalies else 0.0
        values = [v + shift for v in values]
    except Exception as err:
        logger.warning("Klimatologi HK gagal: %s", err)
        values = []
    _clim_cache[key] = (time.monotonic(), values)
    return values


# --- Simulasi ---------------------------------------------------------------------------

def _interp(series: List[Tuple[datetime, float]], ts: datetime) -> Optional[float]:
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        if t0 <= ts <= t1:
            return v0 + (v1 - v0) * (ts - t0).total_seconds() / max((t1 - t0).total_seconds(), 1)
    return None


def simulate(kind: str, now: datetime, observed: Optional[float], latest_at: Optional[datetime],
             latest_temp: Optional[float], day_offset: int = 0, hint: Optional[float] = None,
             rain_expected: bool = False, bias: float = 0.0, paths: Optional[List[Dict[str, Any]]] = None,
             clim: Optional[List[float]] = None) -> Optional[Dict[str, Any]]:
    """Sampel suhu akhir (max atau min hari itu) dari semua sumber. None bila tidak ada jalur sama sekali."""
    from app.paper_trading.hk_bot import sigma_for
    from app.paper_trading.hko_hourly import bias_weight

    local = now.astimezone(HKT)
    day = local.date() + timedelta(days=day_offset)
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=HKT)
    start = now if day_offset == 0 else day_start
    end = day_start + timedelta(days=1)
    paths = all_paths() if paths is None else paths
    highest = kind == "highest"
    seed = int(hashlib.sha1(f"{kind}|{day}|{int(now.timestamp() // 600)}".encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)  # stabil selama 10 menit: tampilan & keputusan tidak berkedip

    extremes: List[Tuple[float, float]] = []   # (nilai, jam sejak sekarang)
    n_paths = 0
    for path in paths:
        series = path["series"]
        offset = 0.0
        if latest_at is not None and latest_temp is not None:
            at_obs = _interp(series, latest_at)
            if at_obs is not None:
                offset = latest_temp - at_obs
        hours = [(ts, v) for ts, v in series if start <= ts < end]
        if not hours:
            continue
        n_paths += 1
        leads = [((ts - latest_at).total_seconds() / 3600 if latest_at else 24.0) for ts, _ in hours]
        base = [v + offset * bias_weight(lead) for (_, v), lead in zip(hours, leads)]
        rains = any(path["precip"].get(ts, 0.0) >= 0.5 for ts, _ in hours[:3])
        for _ in range(SIMS_PER_PATH):
            noise, values = 0.0, []
            for i, (lead, v) in enumerate(zip(leads, base)):
                sd = sigma_for(max(lead, 0.25)) * NOISE_SCALE
                noise = NOISE_RHO * noise + math.sqrt(1 - NOISE_RHO ** 2) * rng.gauss(0, sd)
                values.append(v + noise)
            if rain_expected and day_offset == 0 and not rains and rng.random() < RAIN_SHOCK_PROB:
                hit = rng.randrange(0, min(3, len(values)))
                drop = rng.uniform(*RAIN_SHOCK)
                for j in range(hit, len(values)):  # permukaan basah & awan: puncak sesudahnya ikut tertahan
                    values[j] -= drop * math.exp(-(j - hit) / RAIN_RECOVERY_H)
            idx = max(range(len(values)), key=values.__getitem__) if highest else min(range(len(values)), key=values.__getitem__)
            extremes.append((values[idx], (hours[idx][0] - now).total_seconds() / 3600))
    if not extremes:
        return None

    # Prakiraan resmi HKO: geser sebagian ke angkanya (median simulasi → arah angka resmi)
    shift = 0.0
    if hint is not None:
        median = statistics.median(v for v, _ in extremes)
        shift = float(settings.AUTOTRADE_HK_OFFICIAL_WEIGHT) * (float(hint) - median)
    raw = [v + shift for v, _ in extremes]

    def final(v: float) -> float:
        if observed is None:
            return v
        return max(observed, v) if highest else min(observed, v)

    holds = sum(1 for v in raw if (v <= observed if highest else v >= observed)) / len(raw) if observed is not None else 0.0
    future_leads = sorted(lead for (v, lead), r in zip(extremes, raw)
                          if observed is None or (r > observed if highest else r < observed))
    lead_extreme = 0.0 if holds >= 0.5 or not future_leads else max(future_leads[len(future_leads) // 2], 0.0)

    samples_raw = [final(v) for v in raw]
    samples = [final(v + bias) for v in raw]
    # Klimatologi (data historis): porsi kecil, mengecil saat puncak/lembah mendekat
    clim = climatology(day, kind) if clim is None else clim
    w = CLIM_WEIGHT_MAX if day_offset > 0 else CLIM_WEIGHT_MAX * min(1.0, lead_extreme / CLIM_HORIZON_H)
    n_clim = int(round(w / (1 - w) * len(samples))) if clim and w > 0 else 0
    clim_draws = [final(rng.choice(clim)) for _ in range(n_clim)]
    samples += clim_draws
    samples_raw += clim_draws
    mean = sum(samples) / len(samples)
    std = math.sqrt(sum((x - mean) ** 2 for x in samples) / len(samples))
    qs = sorted(samples)
    return {
        "samples": samples, "mean": round(mean, 2), "mean_raw": round(sum(samples_raw) / len(samples_raw), 2),
        "std": round(std, 2), "p10": round(qs[len(qs) // 10], 1), "p50": round(qs[len(qs) // 2], 1),
        "p90": round(qs[(9 * len(qs)) // 10], 1), "n_paths": n_paths, "n_sims": len(extremes), "n_clim": n_clim,
        "clim_weight": round(w, 3), "official_shift": round(shift, 2), "bias": round(bias, 2),
        "rain_shock": bool(rain_expected and day_offset == 0), "p_observed_holds": round(holds, 3),
        "lead_extreme": round(lead_extreme, 2),
    }


def bracket_probs(samples: List[float], brackets: List[Tuple[float, float]], smooth: float = 0.02) -> List[float]:
    """Proporsi sampel per bracket (X°C = [X, X+1)), sedikit dihaluskan agar tak ada peluang 0/100% mutlak."""
    n = len(samples)
    raw = []
    for lo, hi in brackets:
        a = lo
        b = hi + 1 if hi != math.inf else math.inf
        raw.append(sum(1 for x in samples if a <= x < b) / n if n else 0.0)
    k = len(brackets) or 1
    return [(1 - smooth) * p + smooth / k for p in raw]
