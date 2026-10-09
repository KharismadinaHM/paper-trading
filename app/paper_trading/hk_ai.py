"""
AI Hong Kong (Gemini): pandangan BAYANGAN, ringkasan terjadwal, dan tanya-jawab. Tidak menentukan pembelian.

- ai_view(): Gemini membaca konteks (bacaan HKO, proyeksi per jam, prakiraan resmi HKO, model bot, harga
  pasar, posisi, karakteristik iklim HK) dan memberi peluang per bracket max & min + ringkasan. Peluang AI,
  model, dan pasar dicatat bersamaan (hk_forecast_views) lalu dinilai dengan skor Brier setelah hari selesai,
  supaya terlihat sumber mana yang paling akurat sebelum AI boleh ikut memengaruhi keputusan.
- Ringkasan otomatis pada HK_AI_REPORT_HOURS (zona HK_AI_REPORT_TZ), /rangkum kapan saja, /tanya <pertanyaan>.
- Tanpa GEMINI_API_KEY: ringkasan tetap terkirim dengan bagian model saja.

API key hanya dibaca dari .env (header x-goog-api-key, tidak pernah di URL atau log).
"""
import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.paper_trading.hko_alerts import HKT, STATION, _aware

logger = get_logger("hk_ai")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
KNOWLEDGE = Path(__file__).parent / "knowledge" / "hk_karakteristik.md"
SUMMARY_CACHE_SECONDS = 600
MAX_REPLY_CHARS = 3500
SCORE_DAYS = 14
_summary_cache: Dict[str, Any] = {"at": 0.0, "report": None}


class GeminiError(RuntimeError):
    pass


def ai_problems() -> List[str]:
    problems = []
    if not settings.HK_AI_ENABLED:
        problems.append("HK_AI_ENABLED=false")
    if not settings.GEMINI_API_KEY:
        problems.append("GEMINI_API_KEY belum diisi di .env")
    return problems


MAX_OUTPUT_TOKENS = 8192
THINKING_BUDGET = {"json": 1024, "text": 512}  # model 2.5 "berpikir" dari kuota output yang sama: dibatasi


class GeminiTruncated(GeminiError):
    pass


def _supports_thinking(model: str) -> bool:
    return any(tag in model for tag in ("2.5", "-3"))


def _call(prompt: str, system: str, json_mode: bool, timeout: int, thinking: Optional[int]) -> str:
    config: Dict[str, Any] = {"temperature": 0.2, "maxOutputTokens": MAX_OUTPUT_TOKENS}
    if json_mode:
        config["responseMimeType"] = "application/json"
    if thinking is not None and _supports_thinking(settings.GEMINI_MODEL):
        config["thinkingConfig"] = {"thinkingBudget": thinking}
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": config,
    }
    req = urllib.request.Request(
        GEMINI_URL.format(model=settings.GEMINI_MODEL), data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": str(settings.GEMINI_API_KEY)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = ""
        try:
            detail = json.loads(err.read().decode("utf-8")).get("error", {}).get("message", "")[:200]
        except Exception:
            pass
        raise GeminiError(f"Gemini HTTP {err.code}: {detail}".strip()) from None
    except Exception as err:
        raise GeminiError(f"Gemini tidak bisa dihubungi: {type(err).__name__}") from None
    candidate = (data.get("candidates") or [{}])[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    reason = candidate.get("finishReason")
    if reason == "MAX_TOKENS":
        usage = data.get("usageMetadata") or {}
        logger.warning("Gemini terpotong (MAX_TOKENS): output %s token, berpikir %s token",
                       usage.get("candidatesTokenCount"), usage.get("thoughtsTokenCount"))
        if json_mode or not text:
            raise GeminiTruncated("Jawaban Gemini terpotong (batas token)")
    if not text:
        raise GeminiError(f"Gemini tidak memberi jawaban ({reason or data.get('promptFeedback')})")
    return text


def gemini(prompt: str, system: str, json_mode: bool = False, timeout: int = 60) -> str:
    """
    Satu panggilan generateContent; teks jawaban. Melempar GeminiError (tanpa API key di pesannya).
    Kuota "berpikir" dibatasi agar jawaban tidak terpotong; bila tetap terpotong, diulang sekali tanpa berpikir.
    """
    if ai_problems():
        raise GeminiError("; ".join(ai_problems()))
    try:
        return _call(prompt, system, json_mode, timeout, THINKING_BUDGET["json" if json_mode else "text"])
    except GeminiTruncated:
        return _call(prompt, system, json_mode, timeout, 0)


def parse_json(raw: str) -> Dict[str, Any]:
    """JSON dari jawaban model: toleran terhadap pagar ``` dan teks di luar objek."""
    text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise GeminiError("Jawaban Gemini bukan JSON yang valid") from None
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            raise GeminiError("Jawaban Gemini bukan JSON yang valid") from None
    if not isinstance(data, dict):
        raise GeminiError("Jawaban Gemini bukan objek JSON")
    return data


def _knowledge() -> str:
    try:
        return KNOWLEDGE.read_text(encoding="utf-8")
    except OSError:
        return ""


SYSTEM = """Kamu analis cuaca untuk market Polymarket suhu harian Hong Kong (stasiun HK Observatory, HKO).
Market diselesaikan dengan suhu tertinggi/terendah resmi HKO hari kalender HKT; bracket "X°C" berarti X.0–X.9°C.
Aturan:
- Pakai HANYA data di konteks dan pengetahuan iklim terlampir. Jangan mengarang angka, berita, atau data lain.
- Angka yang sudah terukur hari ini tidak bisa berubah arah: max tidak bisa turun, min tidak bisa naik.
- Nyatakan ketidakpastian sebagai kisaran dan peluang, bukan satu angka pasti.
- Jawab dalam Bahasa Indonesia, ringkas dan jelas. Ini informasi, bukan saran finansial.

Pengetahuan karakteristik suhu Hong Kong:
"""


def context(now: Optional[datetime] = None, analysis: Optional[Dict[str, Any]] = None,
            status: Optional[Dict[str, Any]] = None, tomorrow: Optional[Dict[str, Any]] = None,
            forecast: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Konteks ringkas untuk AI dan laporan: bacaan, tren, prakiraan resmi, model, pasar, posisi."""
    from app.paper_trading import hk_bot
    from app.paper_trading.hko_alerts import hko_status, readings_today

    now = now or datetime.now(timezone.utc)
    status = status or hko_status(now=now)
    if not status:
        return {"error": "Belum ada bacaan HKO hari ini"}
    analysis = analysis or hk_bot.analyze(now, status=status)
    db = get_db_session()
    try:
        rows = readings_today(db, now)
    finally:
        db.close()
    series = [(_aware(r.observed_at).astimezone(HKT), float(r.temp)) for r in rows]
    half_hourly = [f"{ts:%H:%M} {t:.1f}" for ts, t in series if ts.minute in (0, 30)][-16:]
    official = status.get("official") or {}
    local = now.astimezone(HKT)

    def compact(d: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not d:
            return None
        return {"terukur": d.get("observed"), "model_mu": d["mu"], "model_sigma": d["sigma"], "sumber": d["source"],
                "ensemble_per_model": d.get("ensemble"), "sebaran_ensemble": d.get("spread"),
                "koreksi_hujan": d.get("rain") or None, "rezim": d.get("regime"),
                "final": d.get("final"),
                "bracket": [{"bracket": r["bracket"], "model": r["model"], "pasar": r.get("market_prob"),
                             "ask": r.get("ask")} for r in d["brackets"]]}

    try:
        positions = hk_bot.positions_today(now)
    except Exception:
        positions = []
    if forecast is None:
        from app.paper_trading.hk_forecast import forecast_payload
        forecast = forecast_payload(now)
    if tomorrow is None:
        try:
            tomorrow = hk_bot.analyze_tomorrow(now, hours=hk_hours_48(now), fnd=forecast.get("nine_day"))
        except Exception as err:
            logger.warning("Model HK besok gagal: %s", err)
            tomorrow = None
    cuaca = forecast.get("now") or {}
    return {
        "waktu_hkt": f"{local:%Y-%m-%d %H:%M} ({calendar_name(local)})",
        "bacaan_terakhir": {"jam": status["observed_at"].strftime("%H:%M"), "suhu": status["temp"],
                            "max_hari_ini": status["max"], "min_hari_ini": status["min"],
                            "laju_per_jam": status.get("rate")},
        "bacaan_30_menit": half_hourly,
        "proyeksi_per_jam": [f"{p['at']} {p['value']:.1f}" for p in (analysis or {}).get("projection", [])],
        "prakiraan_resmi_hko": (official.get("text") or "")[:900],
        "periode_prakiraan": official.get("period"),
        "peringatan_sangat_panas": official.get("very_hot_warning"),
        "model_bot": {"max": compact((analysis or {}).get("max")), "min": compact((analysis or {}).get("min"))},
        "posisi_paper_hk": positions,
        "cuaca_sekarang_hko": {k: cuaca.get(k) for k in ("text", "humidity", "rain_max_mm", "uv", "warnings")} if cuaca else None,
        "nowcast_hujan_2_jam": [f"s/d {x['end'][11:16]}: stasiun {x['near_mm']} mm, ±10 km maks {x['area_max_mm']} mm"
                                for x in ((forecast.get("nowcast") or {}).get("steps") or [])],
        "peringatan_hko": [w["name"] for w in forecast.get("warnings") or []],
        "rezim_cuaca": forecast.get("regime"),
        "per_jam_12_jam": [f"{h['hour']} {h['temp']:.1f}°C {h['text']} hujan {h.get('rain_prob')}% RH {h.get('humidity')}%"
                           for h in (forecast.get("hours") or [])[:12]],
        "prakiraan_9_hari_hko": [{k: d.get(k) for k in ("date", "max", "min", "weather", "psr")}
                                 for d in ((forecast.get("nine_day") or {}).get("days") or [])[:3]],
        "model_bot_besok": {"tanggal": (tomorrow or {}).get("day"), "max": compact((tomorrow or {}).get("max")),
                            "min": compact((tomorrow or {}).get("min"))} if tomorrow else None,
    }


def hk_hours_48(now: datetime) -> List[Dict[str, Any]]:
    from app.paper_trading.hk_forecast import hourly_outlook
    return hourly_outlook(now, hours=48)


def calendar_name(local: datetime) -> str:
    days = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
    return days[local.weekday()]


def _normalize(probs: Dict[str, Any], labels: List[str]) -> Dict[str, float]:
    clean = {}
    for label in labels:
        try:
            v = float(probs.get(label, 0) or 0)
        except (TypeError, ValueError):
            v = 0.0
        clean[label] = max(v, 0.0)
    total = sum(clean.values())
    if total <= 0:
        return {}
    return {k: round(v / total, 4) for k, v in clean.items()}


def ai_view(now: Optional[datetime] = None, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Pandangan Gemini: {'max': {'point', 'probs'}, 'min': {...}, 'ringkasan', 'alasan', 'risiko'}."""
    now = now or datetime.now(timezone.utc)
    ctx = ctx or context(now)
    labels = {k: [b["bracket"] for b in ((ctx.get("model_bot") or {}).get(k) or {}).get("bracket", [])]
              for k in ("max", "min")}
    tomorrow = ctx.get("model_bot_besok") or {}
    labels_tomorrow = {k: [b["bracket"] for b in (tomorrow.get(k) or {}).get("bracket", [])] for k in ("max", "min")}
    prompt = (
        "Konteks data saat ini (JSON):\n" + json.dumps(ctx, ensure_ascii=False, default=str) + "\n\n"
        "Tugas:\n1. Perkirakan suhu MAX dan MIN resmi HKO HARI INI (hari kalender HKT). Beri peluang untuk SETIAP "
        "bracket berikut (jumlah = 1):\n"
        f"max: {labels['max']}\nmin: {labels['min']}\n"
        "2. Perkirakan juga MAX dan MIN BESOK "
        f"({tomorrow.get('tanggal') or '-'}) dengan peluang per bracket: max: {labels_tomorrow['max']} · min: {labels_tomorrow['min']}\n"
        "3. Prediksi suhu HKO dan kondisi cuaca untuk 6 jam ke depan (per jam).\n"
        "Bandingkan dengan model bot dan harga pasar; jelaskan bila kamu berbeda pendapat.\n"
        "Ringkas: di 'peluang' cantumkan hanya bracket dengan peluang ≥ 0.01 (sisanya dianggap 0); alasan maks 3 poin pendek.\n"
        'Balas JSON saja: {"max": {"perkiraan": angka, "peluang": {"<bracket>": angka}}, '
        '"min": {"perkiraan": angka, "peluang": {"<bracket>": angka}}, '
        '"besok": {"max": {"perkiraan": angka, "peluang": {...}}, "min": {"perkiraan": angka, "peluang": {...}}}, '
        '"per_jam": [{"jam": "HH:MM", "suhu": angka, "cuaca": "singkat"}], '
        '"ringkasan": "2-3 kalimat kondisi saat ini", "alasan": ["..."], "risiko": ["..."]}'
    )
    data = parse_json(gemini(prompt, SYSTEM + _knowledge(), json_mode=True))
    out: Dict[str, Any] = {"ringkasan": str(data.get("ringkasan") or "")[:600],
                           "alasan": [str(x)[:200] for x in (data.get("alasan") or [])][:4],
                           "risiko": [str(x)[:200] for x in (data.get("risiko") or [])][:3]}
    def parse(part: Dict[str, Any], names: List[str]) -> Dict[str, Any]:
        try:
            point = float(part.get("perkiraan"))
        except (TypeError, ValueError):
            point = None
        return {"point": point, "probs": _normalize(part.get("peluang") or {}, names)}

    for k in ("max", "min"):
        out[k] = parse(data.get(k) or {}, labels[k])
    besok = data.get("besok") or {}
    out["besok"] = {k: parse(besok.get(k) or {}, labels_tomorrow[k]) for k in ("max", "min")}
    hourly = []
    for h in data.get("per_jam") or []:
        try:
            hourly.append({"jam": str(h.get("jam"))[:5], "suhu": round(float(h.get("suhu")), 1),
                           "cuaca": str(h.get("cuaca") or "")[:60]})
        except (TypeError, ValueError, AttributeError):
            continue
    out["per_jam"] = hourly[:8]
    return out


# --- Pencatatan & penilaian -------------------------------------------------------------

def record_views(now: datetime, analysis: Dict[str, Any], view: Optional[Dict[str, Any]],
                 tomorrow: Optional[Dict[str, Any]] = None) -> None:
    """Simpan peluang model, pasar, dan AI (bila ada) — hari ini & besok — untuk dinilai setelah harinya selesai."""
    from app.paper_trading.models import HkForecastView

    today = now.astimezone(HKT).date()
    db = get_db_session()
    try:
        if view and view.get("per_jam"):
            db.add(HkForecastView(created_at=now, local_date=today.isoformat(), kind="hourly", source="ai",
                                  probs=json.dumps(view["per_jam"]), summary=view.get("ringkasan")))
        targets = [(today.isoformat(), analysis, view)]
        if tomorrow and tomorrow.get("day"):
            targets.append((tomorrow["day"], tomorrow, (view or {}).get("besok")))
        for local_date, dist, ai_part in targets:
            _record_day(db, now, local_date, dist, ai_part, (view or {}).get("ringkasan") if dist is analysis else None)
        db.commit()
    except Exception as err:
        db.rollback()
        logger.warning("Gagal menyimpan pandangan HK: %s", err)
    finally:
        db.close()


def _record_day(db, now: datetime, local_date: str, analysis: Dict[str, Any], view: Optional[Dict[str, Any]],
                summary: Optional[str]) -> None:
    """Peluang model/pasar/AI satu hari (max & min) — hanya bracket yang ada market-nya (bisa dinilai)."""
    from app.paper_trading.models import HkForecastView

    for k in ("max", "min"):
        d = analysis.get(k)
        if not d:
            continue
        labels = [b["bracket"] for b in d["brackets"] if b.get("market_prob") is not None]
        if not labels:
            continue  # tanpa market hari ini tidak ada yang bisa dinilai
        sources = {
            "model": ({b["bracket"]: b["model"] for b in d["brackets"] if b["bracket"] in labels}, d["mu"]),
            "market": ({b["bracket"]: b["market_prob"] for b in d["brackets"] if b["bracket"] in labels}, None),
        }
        if view and view.get(k, {}).get("probs"):
            sources["ai"] = (view[k]["probs"], view[k].get("point"))
        for source, (probs, point) in sources.items():
            db.add(HkForecastView(created_at=now, local_date=local_date, kind=k, source=source,
                                  probs=json.dumps(probs), point=Decimal(str(round(point, 2))) if point is not None else None,
                                  summary=summary if source == "ai" else None))


def actual_extremes(day: date) -> Optional[Dict[str, float]]:
    """Max & min HKO tercatat untuk satu hari HKT (dari bacaan 10 menit tersimpan), None bila tidak lengkap."""
    from app.paper_trading.models import StationReading

    start = datetime.combine(day, datetime.min.time(), tzinfo=HKT)
    db = get_db_session()
    try:
        rows = (db.query(StationReading).filter(StationReading.station == STATION,
                                                StationReading.observed_at >= start.astimezone(timezone.utc),
                                                StationReading.observed_at <= (start + timedelta(days=1)).astimezone(timezone.utc))
                .all())  # termasuk bacaan 00:00 besoknya: max/min resmi penutupan hari ini
    finally:
        db.close()
    from app.paper_trading.hko_alerts import row_extremes

    own = [r for r in rows if _aware(r.observed_at) < start + timedelta(days=1)]
    if len(own) < 100:  # hari tidak lengkap (layanan mati): jangan dinilai
        return None
    highs, lows = [float(r.temp) for r in own], [float(r.temp) for r in own]
    for r in rows:
        d, mx, mn = row_extremes(r)
        if d == day:
            highs += [mx] if mx is not None else []
            lows += [mn] if mn is not None else []
    return {"max": max(highs), "min": min(lows)}


def scorecard(days: int = SCORE_DAYS, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Skor Brier (makin kecil makin baik) & ketepatan bracket teratas per sumber, hari yang sudah selesai."""
    from app.paper_trading.hko_alerts import bracket_for
    from app.paper_trading.models import HkForecastView

    now = now or datetime.now(timezone.utc)
    today = now.astimezone(HKT).date()
    first = (today - timedelta(days=days)).isoformat()
    db = get_db_session()
    try:
        views = (db.query(HkForecastView).filter(HkForecastView.local_date >= first,
                                                 HkForecastView.local_date < today.isoformat(),
                                                 HkForecastView.kind.in_(("max", "min"))).all())
    finally:
        db.close()
    actual_cache: Dict[str, Optional[Dict[str, float]]] = {}
    acc: Dict[str, Dict[str, Any]] = {}
    for v in views:
        if v.local_date not in actual_cache:
            actual_cache[v.local_date] = actual_extremes(date.fromisoformat(v.local_date))
        actual = actual_cache[v.local_date]
        if not actual:
            continue
        probs = json.loads(v.probs or "{}")
        if not probs:
            continue
        hit = bracket_for(actual[v.kind], list(probs))
        if hit is None:
            continue
        brier = sum((p - (1.0 if label == hit else 0.0)) ** 2 for label, p in probs.items())
        a = acc.setdefault(v.source, {"n": 0, "brier": 0.0, "top": 0, "days": set()})
        a["n"] += 1
        a["brier"] += brier
        a["top"] += max(probs, key=probs.get) == hit
        a["days"].add(v.local_date)
    return {src: {"n": a["n"], "days": len(a["days"]), "brier": round(a["brier"] / a["n"], 3),
                  "top_hit": round(a["top"] / a["n"], 3)} for src, a in acc.items() if a["n"]}


# --- Ringkasan, laporan terjadwal, dan tanya-jawab ----------------------------------------

def _md(text: Any) -> str:
    from app.paper_trading.wallet_bot import md
    return md(text)


def _probs_line(probs: Dict[str, float], top: int = 3) -> str:
    ranked = sorted(probs.items(), key=lambda kv: -kv[1])[:top]
    return " · ".join(f"{label} {p * 100:.0f}%" for label, p in ranked if p > 0)


def build_summary(now: Optional[datetime] = None, use_cache: bool = True, record: bool = True) -> Dict[str, Any]:
    """Ringkasan kejadian saat ini: data, model, AI (bila tersedia), posisi, skor. {'text', 'analysis', 'view', ...}."""
    from app.paper_trading import hk_bot
    from app.paper_trading.hko_alerts import hko_status

    now = now or datetime.now(timezone.utc)
    if use_cache and _summary_cache["report"] and time.monotonic() - _summary_cache["at"] < SUMMARY_CACHE_SECONDS:
        return _summary_cache["report"]
    status = hko_status(now=now)
    if not status:
        return {"text": "🇭🇰 Belum ada bacaan HKO hari ini.", "analysis": None, "view": None, "error": None}
    from app.paper_trading.hk_forecast import forecast_payload

    analysis = hk_bot.analyze(now, status=status)
    forecast = forecast_payload(now)
    try:
        tomorrow = hk_bot.analyze_tomorrow(now, hours=hk_hours_48(now), fnd=forecast.get("nine_day"))
    except Exception as err:
        logger.warning("Model HK besok gagal: %s", err)
        tomorrow = None
    ctx = context(now, analysis=analysis, status=status, tomorrow=tomorrow, forecast=forecast)
    view, error = None, None
    try:
        view = ai_view(now, ctx)
    except GeminiError as err:
        error = str(err)
        logger.warning("Pandangan AI HK gagal: %s", err)
    if record and analysis:
        record_views(now, analysis, view, tomorrow)
    try:
        score = scorecard(now=now)
    except Exception as err:
        logger.warning("Gagal menghitung skor HK: %s", err)
        score = {}
    text = format_summary(now, status, analysis, view, error, ctx.get("posisi_paper_hk") or [], score,
                          forecast=forecast, tomorrow=tomorrow)
    report = {"text": text, "analysis": analysis, "view": view, "error": error, "score": score, "tomorrow": tomorrow,
              "created_at": now.isoformat()}
    _summary_cache.update(at=time.monotonic(), report=report)
    return report


def format_summary(now: datetime, status: Dict[str, Any], analysis: Optional[Dict[str, Any]],
                   view: Optional[Dict[str, Any]], error: Optional[str], positions: List[Dict[str, Any]],
                   score: Dict[str, Any], forecast: Optional[Dict[str, Any]] = None,
                   tomorrow: Optional[Dict[str, Any]] = None) -> str:
    """Pesan Telegram (Markdown v1, teks dinamis di-escape)."""
    from app.paper_trading.hk_bot import format_analysis_lines

    tz = ZoneInfo(settings.HK_AI_REPORT_TZ)
    local = now.astimezone(tz)
    hkt = now.astimezone(HKT)
    rate = status.get("rate")
    tz_label = settings.NOTIFY_TIMEZONE_LABEL if settings.HK_AI_REPORT_TZ == settings.NOTIFY_TIMEZONE else settings.HK_AI_REPORT_TZ
    lines = [f"🇭🇰 *RINGKASAN HONG KONG* · {local:%H:%M} {_md(tz_label)} ({hkt:%H:%M} HKT)",
             f"Suhu {status['temp']:.1f}°C ({status['observed_at']:%H:%M}) · max sejauh ini {status['max']:.1f} · "
             f"min {status['min']:.1f}" + (f" · tren {rate:+.1f}°C/jam" if rate is not None else "")]
    cuaca = (forecast or {}).get("now") or {}
    if cuaca:
        lines.append(f"{cuaca.get('icon', '')} {_md(cuaca.get('text', '-'))} · RH {cuaca.get('humidity', '-')}%"
                     + (f" · hujan {cuaca['rain_max_mm']} mm" if cuaca.get("rain_max_mm") else "")
                     + (f" · ⚠️ {_md('; '.join(cuaca['warnings'])[:120])}" if cuaca.get("warnings") else ""))
    hours = ((forecast or {}).get("hours") or [])[:6:2]
    if hours:
        lines.append("Jam depan: " + " · ".join(f"{h['hour']} {h['icon']} {h['temp']:.1f}°" for h in hours))
    official = (status.get("official") or {}).get("text")
    if official:
        lines.append(f"📋 HKO: {_md(official[:220])}{'…' if len(official) > 220 else ''}")
    if analysis:
        lines.append("")
        lines.append("📈 *Model bot*")
        lines.extend(_md(x) for x in format_analysis_lines(analysis))
    lines.append("")
    if view:
        lines.append("🤖 *AI (Gemini, bayangan)*")
        if view.get("ringkasan"):
            lines.append(_md(view["ringkasan"]))
        for k, title in (("max", "Max"), ("min", "Min")):
            part = view.get(k) or {}
            if part.get("probs"):
                point = f" ~{part['point']:.1f}°C" if part.get("point") is not None else ""
                lines.append(f"{title}{point}: {_md(_probs_line(part['probs']))}")
        if view.get("per_jam"):
            lines.append("Per jam: " + _md(" · ".join(f"{h['jam']} {h['suhu']:.1f}° {h['cuaca']}" for h in view["per_jam"][:6])))
        besok = view.get("besok") or {}
        if any((besok.get(k) or {}).get("probs") for k in ("max", "min")):
            parts = []
            for k, title in (("max", "max"), ("min", "min")):
                part = besok.get(k) or {}
                if part.get("probs"):
                    parts.append(f"{title} {_md(_probs_line(part['probs'], top=2))}")
            lines.append("Besok: " + " · ".join(parts))
        for reason in view.get("alasan") or []:
            lines.append(f"• {_md(reason)}")
        if view.get("risiko"):
            lines.append("⚠️ " + _md("; ".join(view["risiko"])))
    else:
        lines.append(f"🤖 AI tidak tersedia: {_md(error or '-')}")
    if tomorrow and (tomorrow.get("max") or tomorrow.get("min")):
        mx, mn = tomorrow.get("max") or {}, tomorrow.get("min") or {}
        lines.append(f"📅 Model besok ({tomorrow['day'][5:]}): "
                     + (f"max {mx['mu']:.1f}±{mx['sigma']:.1f}°" if mx else "")
                     + (f" · min {mn['mu']:.1f}±{mn['sigma']:.1f}°" if mn else ""))
    if positions:
        lines.append("")
        lines.append("💼 *Posisi paper HK*")
        for p in positions[:5]:
            lines.append(f"• {_md(p['market'][-40:])} · {p['side']} {p['shares']:.1f} sh @ {p['entry'] * 100:.0f}¢")
    if score:
        names = {"ai": "AI", "model": "Model", "market": "Pasar"}
        parts = [f"{names.get(s, s)} {v['brier']:.2f} (tepat {v['top_hit'] * 100:.0f}%)" for s, v in
                 sorted(score.items(), key=lambda kv: kv[1]["brier"])]
        days = max(v["days"] for v in score.values())
        lines.append("")
        lines.append(f"🎯 Akurasi {days} hari (Brier, kecil = baik): " + " · ".join(parts))
    lines.append("_Info, bukan saran finansial._")
    return "\n".join(lines)


def report_hours() -> List[int]:
    out = []
    for x in str(settings.HK_AI_REPORT_HOURS).split(","):
        x = x.strip()
        if x.isdigit() and 0 <= int(x) <= 23:
            out.append(int(x))
    return sorted(set(out))


def maybe_send_scheduled(now: Optional[datetime] = None) -> bool:
    """Kirim ringkasan pada jam laporan (sekali per slot, dalam 30 menit pertama jam itu)."""
    from app.paper_trading.autotrader import _get_state, _set_state
    from app.paper_trading.telegram import send_telegram_message

    now = now or datetime.now(timezone.utc)
    if not settings.HK_AI_ENABLED:
        return False
    local = now.astimezone(ZoneInfo(settings.HK_AI_REPORT_TZ))
    if local.hour not in report_hours() or local.minute >= 30:
        return False
    key = f"hkai|{local:%Y%m%d%H}"
    if _get_state(key):
        return False
    _set_state(key, "1", now)
    report = build_summary(now, use_cache=False)
    result = send_telegram_message(report["text"], parse_mode="Markdown")
    if not result.get("success"):
        logger.warning("Ringkasan HK tidak terkirim (%s); kirim ulang tanpa format", result.get("error"))
        result = send_telegram_message(report["text"].replace("\\", "").replace("*", "").replace("_", ""))
    return bool(result.get("success"))


def run_hk_ai() -> None:
    """Dipanggil dari loop collector; tidak pernah melempar exception."""
    try:
        maybe_send_scheduled()
    except Exception as err:
        logger.error("Ringkasan AI Hong Kong gagal: %s", err, exc_info=True)


def _ask_allowed(now: datetime) -> bool:
    from app.paper_trading.autotrader import _get_state, _set_state

    key = f"hkask|{now:%Y%m%d%H}"
    used = int(_get_state(key) or 0)
    if used >= settings.HK_AI_ASK_PER_HOUR:
        return False
    _set_state(key, str(used + 1), now)
    return True


def ask(question: str, now: Optional[datetime] = None) -> str:
    """Jawaban Gemini untuk pertanyaan bebas tentang market HK hari ini, berdasar data terkini."""
    now = now or datetime.now(timezone.utc)
    question = (question or "").strip()
    if not question:
        return "Tulis pertanyaannya, misalnya: /tanya berapa peluang max ≥ 30°C hari ini?"
    if ai_problems():
        return "AI belum aktif: " + "; ".join(ai_problems())
    if not _ask_allowed(now):
        return f"Batas {settings.HK_AI_ASK_PER_HOUR} pertanyaan per jam tercapai. Coba lagi nanti."
    ctx = context(now)
    try:
        score = scorecard(now=now)
    except Exception:
        score = {}
    prompt = ("Konteks data saat ini (JSON):\n" + json.dumps({**ctx, "akurasi_14_hari": score}, ensure_ascii=False, default=str)
              + f"\n\nPertanyaan pengguna: {question[:1000]}\n\n"
              "Jawab ringkas (maks ±200 kata), sebut angka dari konteks bila relevan, dan katakan terus terang "
              "bila datanya tidak cukup untuk menjawab.")
    try:
        answer = gemini(prompt, SYSTEM + _knowledge())
    except GeminiError as err:
        return f"AI gagal menjawab: {err}"
    return answer[:MAX_REPLY_CHARS]


def latest_views(now: Optional[datetime] = None, day: Optional[str] = None) -> Dict[str, Any]:
    """Pandangan terbaru tiap sumber untuk satu hari HKT (default hari ini) — max, min, dan AI per jam."""
    from app.paper_trading.models import HkForecastView

    now = now or datetime.now(timezone.utc)
    day = day or now.astimezone(HKT).date().isoformat()
    db = get_db_session()
    try:
        rows = (db.query(HkForecastView).filter(HkForecastView.local_date == day)
                .order_by(HkForecastView.created_at.desc()).limit(60).all())
    finally:
        db.close()
    out: Dict[str, Any] = {}
    for r in rows:
        slot = out.setdefault(r.kind, {})
        if r.source not in slot:
            slot[r.source] = {"probs": json.loads(r.probs or "{}"), "point": float(r.point) if r.point is not None else None,
                              "summary": r.summary, "created_at": _aware(r.created_at).isoformat()}
    return out

