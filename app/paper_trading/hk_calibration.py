"""
Kalibrasi otomatis harian bot Hong Kong (setelah hari HKT selesai, sekali sehari).

Yang dipelajari dari sinyal hk_max / hk_min yang tercatat tiap 30 menit (autotrade_signals):

1. Bias titik perkiraan per jam HKT (per kelompok jam): rata-rata (hasil resmi − perkiraan mentah), dengan
   perkiraan = max(terukur, puncak sisa hari) untuk max dan min(terukur, lembah) untuk min. Perkiraan MENTAH
   (sebelum koreksi) disimpan di sinyal (features.mu_raw), jadi koreksi tidak menghitung dirinya sendiri.
2. Penyesuaian per rezim cuaca saat perkiraan dibuat ('hujan' / 'mendung' / 'cerah', dari nowcast radar, peringatan,
   dan tutupan awan/peluang hujan beberapa jam ke depan): rata-rata residu yang tersisa setelah bias per jam.
   Contoh: "saat mendung tebal, max biasanya 0.8°C di bawah proyeksi" → max hari mendung digeser turun.
3. Bobot model vs harga pasar: bobot dengan log-loss terkecil pada sinyal yang sudah resolve.

Pengaman (data sedikit mudah menyesatkan, pelajaran bot BTC):
- dipakai setelah ≥ MIN_DAYS hari data; sebelum itu 0 / bobot default;
- ditarik ke nol / ke bobot default sesuai jumlah hari (shrinkage);
- dibatasi (bias ±BIAS_CAP °C) dan berubah paling banyak MAX_BIAS_STEP / MAX_WEIGHT_STEP per hari;
- tiap hari hari dirata-rata dulu (sinyal dalam satu hari saling berkorelasi);
- bobot yang diatur manual di dashboard selalu menang; HK_AUTO_CALIBRATE=false mematikan semuanya; reset kapan saja.
"""
import json
import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.hko_alerts import HKT

logger = get_logger("hk_calibration")

STATE_KEY = "hk_calibration"
WINDOW_DAYS = 30
MIN_DAYS = 10
BIAS_SHRINK_DAYS = 5          # bias = rata-rata × n / (n + 5)
BIAS_CAP = 1.5                # °C
MAX_BIAS_STEP = 0.3           # °C per hari
WEIGHT_SHRINK_DAYS = 20       # bobot = default + (terbaik − default) × n / (n + 20)
MAX_WEIGHT_STEP = 0.1
WEIGHT_RANGE = (0.1, 1.0)
RUN_AFTER_HKT = (0, 30)       # jalan sekali sehari setelah 00:30 HKT
REGIMES = ("hujan", "mendung", "cerah")
MIN_REGIME_DAYS = 8           # penyesuaian rezim dipakai setelah ≥ 8 hari rezim itu
BUCKETS = [(0, 6, "00–06"), (6, 9, "06–09"), (9, 12, "09–12"), (12, 15, "12–15"), (15, 18, "15–18"), (18, 24, "18–24")]
_cache: Dict[str, Any] = {"at": 0.0, "value": None}


def bucket_of(hour: float) -> str:
    for lo, hi, label in BUCKETS:
        if lo <= hour < hi:
            return label
    return BUCKETS[-1][2]


def is_enabled() -> bool:
    from app.paper_trading.autotrader import cfg
    return bool(cfg("HK_AUTO_CALIBRATE"))


def current() -> Dict[str, Any]:
    """Kalibrasi tersimpan (cache 60 detik); {} bila belum pernah dihitung atau di-reset."""
    from app.paper_trading.autotrader import _get_state

    if _cache["value"] is not None and time.monotonic() - _cache["at"] < 60:
        return _cache["value"]
    try:
        value = json.loads(_get_state(STATE_KEY) or "{}")
    except ValueError:
        value = {}
    _cache.update(at=time.monotonic(), value=value)
    return value


def _store(value: Dict[str, Any], now: datetime) -> None:
    from app.paper_trading.autotrader import _set_state
    _set_state(STATE_KEY, json.dumps(value), now)
    _cache.update(at=0.0, value=None)


def bias_for(kind: str, hour: float, regime: Optional[str] = None) -> float:
    """
    Koreksi °C untuk perkiraan 'max'/'min' yang dibuat pada jam HKT ini: bias per kelompok jam + penyesuaian
    rezim cuaca ('hujan' / 'mendung' / 'cerah'). 0 bila nonaktif / belum cukup data.
    """
    if not is_enabled():
        return 0.0
    cal = current()
    entry = ((cal.get("bias") or {}).get(kind) or {}).get(bucket_of(hour)) or {}
    adj = ((cal.get("regime") or {}).get(kind) or {}).get(regime or "") or {}
    return float(entry.get("value") or 0.0) + float(adj.get("value") or 0.0)


def model_weight() -> float:
    """Bobot model vs pasar: override dashboard > hasil kalibrasi (bila aktif) > default .env."""
    from app.paper_trading.autotrader import _config_overrides, cfg

    if "HK_MODEL_WEIGHT" in _config_overrides():
        return float(cfg("HK_MODEL_WEIGHT"))
    weight = (current().get("weight") or {}).get("value")
    if is_enabled() and weight is not None:
        return float(weight)
    return float(settings.AUTOTRADE_HK_MODEL_WEIGHT)


# --- Data -------------------------------------------------------------------------------

def load_rows(now: datetime, days: int = WINDOW_DAYS) -> List[Dict[str, Any]]:
    """Sinyal HK hari-hari yang sudah selesai (HKT), dengan fitur yang dibutuhkan kalibrasi."""
    from app.paper_trading.models import AutotradeSignal

    today = now.astimezone(HKT).date()
    start = datetime.combine(today - timedelta(days=days), datetime.min.time(), tzinfo=HKT)
    end = datetime.combine(today, datetime.min.time(), tzinfo=HKT)
    db = get_db_session()
    try:
        signals = (db.query(AutotradeSignal)
                   .filter(AutotradeSignal.strategy.in_(("hk_max", "hk_min")),
                           AutotradeSignal.created_at >= start.astimezone(timezone.utc),
                           AutotradeSignal.created_at < end.astimezone(timezone.utc))
                   .order_by(AutotradeSignal.created_at).all())
    finally:
        db.close()
    rows = []
    for s in signals:
        try:
            f = json.loads(s.features or "{}")
        except ValueError:
            continue
        created = s.created_at if s.created_at.tzinfo else s.created_at.replace(tzinfo=timezone.utc)
        local = created.astimezone(HKT)
        mu_raw = f.get("mu_raw", f.get("mu"))  # sinyal lama (sebelum kalibrasi) belum punya mu_raw: mu = mentah
        rows.append({
            "kind": "max" if s.strategy == "hk_max" else "min", "day": local.date(),
            "hour": f.get("hour_hkt", local.hour + local.minute / 60), "mu_raw": mu_raw, "observed": f.get("observed"),
            "model": f.get("model_raw"), "market": f.get("market_prob"), "outcome": s.outcome,
            "regime": f.get("regime"),
        })
    return rows


def _residuals(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """(hasil resmi − perkiraan mentah) per sinyal; perkiraan = max(terukur, puncak) / min(terukur, lembah)."""
    from app.paper_trading.hk_ai import actual_extremes

    actuals: Dict[date, Optional[Dict[str, float]]] = {}
    out = []
    for r in rows:
        if r["mu_raw"] is None or r["observed"] is None:
            continue
        if r["day"] not in actuals:
            actuals[r["day"]] = actual_extremes(r["day"])
        actual = actuals[r["day"]]
        if not actual:
            continue
        point = max(r["observed"], r["mu_raw"]) if r["kind"] == "max" else min(r["observed"], r["mu_raw"])
        out.append({**r, "bucket": bucket_of(r["hour"]), "resid": actual[r["kind"]] - point})
    return out


def _shrunk_step(daily: List[float], min_days: int, old: float) -> Dict[str, Any]:
    n = len(daily)
    mean = sum(daily) / n if n else 0.0
    target = mean * n / (n + BIAS_SHRINK_DAYS) if n >= min_days else 0.0
    target = max(-BIAS_CAP, min(BIAS_CAP, target))
    value = old + max(-MAX_BIAS_STEP, min(MAX_BIAS_STEP, target - old))
    return {"value": round(value, 2), "target": round(target, 2), "mean": round(mean, 2), "days": n}


def _bias_table(resid: List[Dict[str, Any]], previous: Dict[str, Any]) -> Dict[str, Any]:
    per: Dict[tuple, Dict[date, List[float]]] = {}
    for r in resid:
        per.setdefault((r["kind"], r["bucket"]), {}).setdefault(r["day"], []).append(r["resid"])
    table: Dict[str, Dict[str, Any]] = {"max": {}, "min": {}}
    for kind in ("max", "min"):
        for _, _, label in BUCKETS:
            daily = [sum(v) / len(v) for v in per.get((kind, label), {}).values()]
            old = float((((previous.get("bias") or {}).get(kind) or {}).get(label) or {}).get("value") or 0.0)
            table[kind][label] = _shrunk_step(daily, MIN_DAYS, old)
    return table


def _regime_table(resid: List[Dict[str, Any]], bias: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, Any]:
    """Penyesuaian per rezim cuaca: rata-rata residu yang tersisa setelah bias per kelompok jam (rata-rata mentahnya)."""
    per: Dict[tuple, Dict[date, List[float]]] = {}
    for r in resid:
        if r.get("regime") not in REGIMES:
            continue
        base = ((bias.get(r["kind"]) or {}).get(r["bucket"]) or {}).get("mean", 0.0)
        per.setdefault((r["kind"], r["regime"]), {}).setdefault(r["day"], []).append(r["resid"] - base)
    table: Dict[str, Dict[str, Any]] = {"max": {}, "min": {}}
    for kind in ("max", "min"):
        for name in REGIMES:
            daily = [sum(v) / len(v) for v in per.get((kind, name), {}).values()]
            old = float((((previous.get("regime") or {}).get(kind) or {}).get(name) or {}).get("value") or 0.0)
            table[kind][name] = _shrunk_step(daily, MIN_REGIME_DAYS, old)
    return table


def _logloss(rows: List[Dict[str, Any]], w: float) -> float:
    total = weight = 0.0
    for r in rows:
        p = min(max(r["market"] + w * (r["model"] - r["market"]), 1e-4), 1 - 1e-4)
        total += r["w"] * -(math.log(p) if r["outcome"] == "WIN" else math.log(1 - p))
        weight += r["w"]
    return total / weight if weight else float("nan")


def _weight(rows: List[Dict[str, Any]], previous: Dict[str, Any]) -> Dict[str, Any]:
    usable = [r for r in rows if r["outcome"] in ("WIN", "LOSS") and r["model"] is not None and r["market"] is not None]
    counts: Dict[tuple, int] = {}
    for r in usable:
        counts[(r["kind"], r["day"])] = counts.get((r["kind"], r["day"]), 0) + 1
    for r in usable:
        r["w"] = 1 / counts[(r["kind"], r["day"])]  # tiap hari & jenis berbobot sama
    prior = float(settings.AUTOTRADE_HK_MODEL_WEIGHT)
    n_days = len({r["day"] for r in usable})
    best = None
    if usable:
        grid = [round(i * 0.05, 2) for i in range(21)]
        best = min(grid, key=lambda w: _logloss(usable, w))
    target = prior + (best - prior) * n_days / (n_days + WEIGHT_SHRINK_DAYS) if best is not None and n_days >= MIN_DAYS else prior
    old = float((previous.get("weight") or {}).get("value") or prior)
    value = old + max(-MAX_WEIGHT_STEP, min(MAX_WEIGHT_STEP, target - old))
    value = max(WEIGHT_RANGE[0], min(WEIGHT_RANGE[1], value))
    return {"value": round(value, 3), "target": round(target, 3), "best": best, "prior": prior, "days": n_days,
            "samples": len(usable),
            "logloss_best": round(_logloss(usable, best), 4) if best is not None else None,
            "logloss_market": round(_logloss(usable, 0.0), 4) if usable else None,
            "logloss_model": round(_logloss(usable, 1.0), 4) if usable else None}


def calibrate(now: Optional[datetime] = None, store: bool = True) -> Dict[str, Any]:
    """Hitung kalibrasi baru dari data WINDOW_DAYS hari terakhir (melangkah dari nilai sebelumnya)."""
    now = now or datetime.now(timezone.utc)
    previous = current()
    rows = load_rows(now)
    result = {"updated_at": now.isoformat(), "window_days": WINDOW_DAYS, "min_days": MIN_DAYS,
              "weight": _weight(rows, previous), "signals": len(rows)}
    resid = _residuals(rows)
    result["bias"] = _bias_table(resid, previous)
    result["regime"] = _regime_table(resid, result["bias"], previous)
    result["min_regime_days"] = MIN_REGIME_DAYS
    if store:
        _store(result, now)
    return result


def reset(now: Optional[datetime] = None) -> None:
    _store({}, now or datetime.now(timezone.utc))


# --- Jadwal & tampilan ------------------------------------------------------------------

def format_status(cal: Optional[Dict[str, Any]] = None, previous: Optional[Dict[str, Any]] = None) -> str:
    """Pesan Telegram (Markdown v1) status kalibrasi; bila previous diberikan, tandai perubahan."""
    cal = current() if cal is None else cal
    state = "aktif" if is_enabled() else "NONAKTIF (HK_AUTO_CALIBRATE)"
    if not cal:
        return (f"🧮 *Kalibrasi otomatis HK* — {state}\nBelum ada kalibrasi. Dihitung tiap hari setelah 00:30 HKT; "
                f"koreksi dipakai setelah ≥ {MIN_DAYS} hari data. `/kalibrasi jalankan` untuk menghitung sekarang.")
    lines = [f"🧮 *Kalibrasi otomatis HK* — {state}",
             f"Data {cal.get('window_days')} hari terakhir · {cal.get('signals', 0)} sinyal · diperbarui "
             f"{datetime.fromisoformat(cal['updated_at']).astimezone(HKT):%d %b %H:%M} HKT"]
    for kind, title in (("max", "Bias max"), ("min", "Bias min")):
        parts = []
        for _, _, label in BUCKETS:
            e = ((cal.get("bias") or {}).get(kind) or {}).get(label) or {}
            if not e.get("days"):
                continue
            old = ((((previous or {}).get("bias") or {}).get(kind) or {}).get(label) or {}).get("value")
            moved = f" (dari {old:+.2f})" if old is not None and abs(old - e["value"]) >= 0.005 else ""
            parts.append(f"{label}: {e['value']:+.2f}°{moved} [{e['days']} hr, mentah {e['mean']:+.2f}]")
        lines.append(f"{title}: " + ("; ".join(parts) if parts else "belum ada data"))
    for kind, title in (("max", "Rezim max"), ("min", "Rezim min")):
        parts = [f"{name} {e['value']:+.2f}° [{e['days']} hr]" for name, e in ((cal.get("regime") or {}).get(kind) or {}).items()
                 if e.get("days")]
        if parts:
            lines.append(f"{title}: " + "; ".join(parts))
    w = cal.get("weight") or {}
    old_w = ((previous or {}).get("weight") or {}).get("value")
    lines.append(f"Bobot model vs pasar: {w.get('value', settings.AUTOTRADE_HK_MODEL_WEIGHT)}"
                 + (f" (dari {old_w})" if old_w is not None and old_w != w.get("value") else "")
                 + (f" · terbaik di data {w['best']} ({w['days']} hari, {w['samples']} sinyal)" if w.get("best") is not None else
                    " · belum ada sinyal resolve"))
    if w.get("logloss_market") is not None:
        lines.append(f"Log-loss (kecil = baik): pasar {w['logloss_market']} · model {w['logloss_model']} · campuran terbaik {w['logloss_best']}")
    lines.append(f"Koreksi dipakai setelah ≥ {MIN_DAYS} hari; maks ±{MAX_BIAS_STEP}°/hari & ±{MAX_WEIGHT_STEP} bobot/hari. "
                 "`/kalibrasi reset` mengembalikan ke default.")
    from app.paper_trading.autotrader import _config_overrides
    if "HK_MODEL_WEIGHT" in _config_overrides():
        lines.append("ℹ️ Bobot diatur manual di dashboard — hasil kalibrasi bobot tidak dipakai.")
    return "\n".join(lines)


def maybe_calibrate(now: Optional[datetime] = None) -> bool:
    """Sekali sehari setelah 00:30 HKT: hitung, simpan, kabarkan ke grup auto trade."""
    from app.paper_trading.autotrader import _get_state, _set_state, notify

    now = now or datetime.now(timezone.utc)
    local = now.astimezone(HKT)
    if (local.hour, local.minute) < RUN_AFTER_HKT or not is_enabled():
        return False
    key = f"hkcal|{local.date().isoformat()}"
    if _get_state(key):
        return False
    _set_state(key, "1", now)
    previous = current()
    cal = calibrate(now)
    notify(format_status(cal, previous).replace("*", "").replace("`", ""))  # notify tanpa Markdown
    return True


def run_hk_calibration() -> None:
    """Dipanggil dari loop collector; tidak pernah melempar exception."""
    try:
        maybe_calibrate()
    except Exception as err:
        logger.error("Kalibrasi HK gagal: %s", err, exc_info=True)
