"""
Registry kategori market yang dikumpulkan oleh Market Collector.

Setiap kategori mendefinisikan dari mana market diambil di Gamma API (tag & query pencarian),
filter judul event, dan label kategori yang disimpan di market_snapshots. Menambah kategori
baru cukup dengan menambahkan entri di `_build_registry()`.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.core.config import settings

WEATHER_SEARCH_QUERIES = ("weather", "temperature", "rain", "snow", "hurricane")


@dataclass(frozen=True)
class MarketCategory:
    key: str
    label: str
    # Tag Gamma API yang dipaginasi (/events?tag_id=...)
    tag_ids: Tuple[str, ...]
    # Query /public-search tambahan
    search_queries: Tuple[str, ...] = ()
    # Event hanya diambil jika judulnya mengandung salah satu kata ini (kosong = semua)
    title_keywords: Tuple[str, ...] = ()
    # Label kategori tetap untuk market_snapshots.category. None = deteksi subkategori cuaca
    fixed_category: Optional[str] = None
    # Filter judul untuk tampilan event berkelompok (kartu dashboard)
    event_title_keywords: Tuple[str, ...] = field(default_factory=tuple)
    # Query /public-search tambahan untuk tampilan event berkelompok
    event_search_queries: Tuple[str, ...] = ()
    # Pasangan (tag_id, order) untuk tampilan event berkelompok. Kosong = setiap tag diurutkan
    # berdasarkan volume24hr. Setiap request bisa berukuran beberapa MB, jadi batasi seperlunya.
    event_tag_orders: Tuple[Tuple[str, str], ...] = ()

    def event_sources(self) -> Tuple[Tuple[str, str], ...]:
        return self.event_tag_orders or tuple((t, "volume24hr") for t in self.tag_ids)

    def matches_title(self, title: Optional[str], keywords: Optional[Tuple[str, ...]] = None) -> bool:
        words = self.title_keywords if keywords is None else keywords
        if not words:
            return True
        text = str(title or "").lower()
        return any(w in text for w in words)


def _split_ids(raw: str) -> Tuple[str, ...]:
    return tuple(t.strip() for t in str(raw).split(",") if t.strip())


def _build_registry() -> Dict[str, MarketCategory]:
    categories = [
        MarketCategory(
            key="weather",
            label="Cuaca",
            tag_ids=_split_ids(settings.WEATHER_TAG_IDS),
            search_queries=WEATHER_SEARCH_QUERIES,
            fixed_category=None,
            event_title_keywords=("temperature",),
            event_search_queries=("Highest temperature", "Lowest temperature"),
            # Kombinasi yang menjaring event hari ini/besok/lusa (menambah kombinasi lain
            # hanya menambah ~1 event namun ~10 detik & ~5 MB per request)
            event_tag_orders=(
                ("84", "volume24hr"),
                ("103040", "startDate"),
                ("103040", "volume24hr"),
                ("104596", "startDate"),
            ),
        ),
        MarketCategory(
            key="elon_tweets",
            label="Elon Musk Tweets",
            # 972 = "Tweet Markets" (juga berisi Trump, CZ, dll. — difilter lewat judul)
            tag_ids=_split_ids(settings.ELON_TWEETS_TAG_IDS),
            search_queries=("elon musk tweets",),
            title_keywords=("elon",),
            fixed_category="Elon Tweets",
            event_title_keywords=("elon",),
            event_search_queries=("elon musk tweets",),
        ),
    ]
    return {c.key: c for c in categories}


def get_category(key: str) -> MarketCategory:
    registry = _build_registry()
    if key not in registry:
        raise KeyError(f"Kategori market '{key}' tidak dikenal. Pilihan: {', '.join(registry)}")
    return registry[key]


def enabled_categories() -> List[MarketCategory]:
    """Kategori yang aktif sesuai ENABLED_MARKET_CATEGORIES (urutan dipertahankan)."""
    registry = _build_registry()
    keys = _split_ids(settings.ENABLED_MARKET_CATEGORIES)
    return [registry[k] for k in keys if k in registry]
