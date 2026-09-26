"""
Data kota market suhu Polymarket: zona waktu IANA dan koordinat perkiraan stasiun resolusi
(umumnya bandara, mis. LaGuardia untuk NYC, Changi untuk Singapore).

Nama kunci = nama kota persis seperti di pertanyaan market ("New York City", "Seoul (Incheon)").
"""
from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class City:
    tz: str
    lat: float
    lon: float


CITIES: Dict[str, City] = {
    "Amsterdam": City("Europe/Amsterdam", 52.31, 4.76),
    "Ankara": City("Europe/Istanbul", 40.13, 32.99),
    "Atlanta": City("America/New_York", 33.64, -84.43),
    "Austin": City("America/Chicago", 30.19, -97.67),
    "Beijing": City("Asia/Shanghai", 40.08, 116.58),
    "Boston": City("America/New_York", 42.36, -71.01),
    "Buenos Aires": City("America/Argentina/Buenos_Aires", -34.56, -58.42),
    "Busan": City("Asia/Seoul", 35.18, 128.94),
    "Cape Town": City("Africa/Johannesburg", -33.97, 18.60),
    "Chengdu": City("Asia/Shanghai", 30.58, 103.95),
    "Chicago": City("America/Chicago", 41.98, -87.90),
    "Chongqing": City("Asia/Shanghai", 29.72, 106.64),
    "Dallas": City("America/Chicago", 32.85, -96.85),
    "Denver": City("America/Denver", 39.86, -104.67),
    "Dubai": City("Asia/Dubai", 25.25, 55.36),
    "Guangzhou": City("Asia/Shanghai", 23.39, 113.30),
    "Helsinki": City("Europe/Helsinki", 60.32, 24.96),
    "Hong Kong": City("Asia/Hong_Kong", 22.31, 114.17),
    "Houston": City("America/Chicago", 29.98, -95.34),
    "Istanbul": City("Europe/Istanbul", 41.26, 28.74),
    "Jakarta": City("Asia/Jakarta", -6.13, 106.66),
    "Jeddah": City("Asia/Riyadh", 21.68, 39.16),
    "Jinan": City("Asia/Shanghai", 36.86, 117.22),
    "Karachi": City("Asia/Karachi", 24.91, 67.16),
    "Kuala Lumpur": City("Asia/Kuala_Lumpur", 2.75, 101.71),
    "London": City("Europe/London", 51.51, 0.05),
    "Los Angeles": City("America/Los_Angeles", 33.94, -118.41),
    "Lucknow": City("Asia/Kolkata", 26.76, 80.89),
    "Madrid": City("Europe/Madrid", 40.47, -3.56),
    "Manila": City("Asia/Manila", 14.51, 121.02),
    "Mexico City": City("America/Mexico_City", 19.44, -99.07),
    "Miami": City("America/New_York", 25.79, -80.29),
    "Milan": City("Europe/Rome", 45.63, 8.72),
    "Moscow": City("Europe/Moscow", 55.97, 37.41),
    "Mumbai": City("Asia/Kolkata", 19.09, 72.87),
    "Munich": City("Europe/Berlin", 48.35, 11.79),
    "New Delhi": City("Asia/Kolkata", 28.57, 77.10),
    "New York City": City("America/New_York", 40.78, -73.87),
    "Panama City": City("America/Panama", 9.07, -79.38),
    "Paris": City("Europe/Paris", 49.01, 2.55),
    "Phoenix": City("America/Phoenix", 33.43, -112.01),
    "Qingdao": City("Asia/Shanghai", 36.27, 120.37),
    "San Francisco": City("America/Los_Angeles", 37.62, -122.37),
    "Sao Paulo": City("America/Sao_Paulo", -23.63, -46.66),
    "Seattle": City("America/Los_Angeles", 47.45, -122.31),
    "Seoul (Incheon)": City("Asia/Seoul", 37.46, 126.44),
    "Shanghai": City("Asia/Shanghai", 31.14, 121.81),
    "Shenzhen": City("Asia/Shanghai", 22.64, 113.81),
    "Singapore": City("Asia/Singapore", 1.36, 103.99),
    "Sydney": City("Australia/Sydney", -33.95, 151.18),
    "Taipei": City("Asia/Taipei", 25.08, 121.23),
    "Tel Aviv": City("Asia/Jerusalem", 32.01, 34.89),
    "Tokyo": City("Asia/Tokyo", 35.55, 139.78),
    "Toronto": City("America/Toronto", 43.68, -79.63),
    "Warsaw": City("Europe/Warsaw", 52.17, 20.97),
    "Washington DC": City("America/New_York", 38.85, -77.04),
    "Wellington": City("Pacific/Auckland", -41.33, 174.81),
    "Wuhan": City("Asia/Shanghai", 30.78, 114.21),
    "Zhengzhou": City("Asia/Shanghai", 34.52, 113.84),
}

# Nama alternatif yang dipakai di judul event (bukan pertanyaan market)
CITY_ALIASES: Dict[str, str] = {
    "NYC": "New York City",
    "Seoul": "Seoul (Incheon)",
}


def resolve_city(name: str) -> str:
    return CITY_ALIASES.get(name, name)
