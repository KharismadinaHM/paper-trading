"""
Perhitungan posisi matahari sederhana (persamaan NOAA) untuk solar noon dan sunrise.

Akurasi sekitar ±1–2 menit untuk lintang non-kutub — lebih dari cukup untuk menentukan
jam puncak suhu harian.
"""
import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple


def _equation_of_time_and_declination(d: date) -> Tuple[float, float]:
    gamma = 2 * math.pi / 365 * (d.timetuple().tm_yday - 1)
    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma)
    )
    return eqtime, decl


def solar_noon_utc(d: date, lon: float) -> datetime:
    """Saat matahari mencapai titik tertinggi (UTC) pada tanggal `d` di bujur `lon` (derajat, timur positif)."""
    eqtime, _ = _equation_of_time_and_declination(d)
    minutes = 720 - 4 * lon - eqtime
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def sunrise_utc(d: date, lat: float, lon: float) -> Optional[datetime]:
    """Waktu matahari terbit (UTC). None jika matahari tidak terbit/terbenam (wilayah kutub)."""
    eqtime, decl = _equation_of_time_and_declination(d)
    lat_r = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(lat_r) * math.cos(decl))
              - math.tan(lat_r) * math.tan(decl))
    if not -1.0 <= cos_ha <= 1.0:
        return None
    ha = math.degrees(math.acos(cos_ha))
    minutes = 720 - 4 * (lon + ha) - eqtime
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(minutes=minutes)
