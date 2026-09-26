"""
Test registry kategori market, pemetaan outcome Up/Down, dan market Elon Musk Tweets.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import app.market_collector.collector as collector
from app.core.config import settings
from app.core.database import get_db_session
from app.market_collector.categories import enabled_categories, get_category
from app.market_collector.collector import (
    determine_winning_outcome,
    fetch_all_markets,
    fetch_category_events,
    fetch_category_markets,
    parse_market_dict,
)
from app.paper_service import create_paper_order, get_market_by_id
from app.paper_trading.models import MarketSnapshot


def _market(cid, question, outcomes='["Yes", "No"]', prices='["0.30", "0.70"]', **extra):
    return {"conditionId": cid, "question": question, "outcomes": outcomes,
            "outcomePrices": prices, "closed": False, **extra}


ELON_EVENT = {
    "id": "ev-elon", "title": "Elon Musk # tweets September 22 - September 29, 2026?", "volume": 516906,
    "endDate": "2026-09-29T16:00:00Z", "slug": "elon-musk-of-tweets-september-22-september-29-2026",
    "markets": [
        _market("0xelon_a", "Will Elon Musk post 200-219 tweets?", prices='["0.25", "0.75"]', groupItemTitle="200-219"),
        _market("0xelon_b", "Will Elon Musk post 220-239 tweets?", prices='["0.40", "0.60"]', groupItemTitle="220-239"),
    ],
}
TRUMP_EVENT = {
    "id": "ev-trump", "title": "Donald Trump # Truth Social posts September 22 - September 29, 2026?",
    "markets": [_market("0xtrump", "Will Trump post 100-119 times?")],
}
WEATHER_EVENT = {
    "id": "ev-weather", "title": "Highest temperature in NYC on September 25?",
    "markets": [_market("0xnyc", "Will NYC be 80°F or higher?")],
}


def _response(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    return resp


def _fake_gamma(req, timeout=None):
    """Gamma API palsu: tag 972 = Tweet Markets, tag lain = cuaca."""
    url = urlparse(req.full_url)
    params = parse_qs(url.query)
    if url.path.endswith("/public-search"):
        q = params.get("q", [""])[0].lower()
        return _response({"events": [ELON_EVENT] if "elon" in q else [WEATHER_EVENT]})
    if params.get("tag_id") == ["972"]:
        return _response([ELON_EVENT, TRUMP_EVENT] if params.get("offset", ["0"]) == ["0"] else [])
    return _response([WEATHER_EVENT] if params.get("offset", ["0"]) == ["0"] else [])


@pytest.fixture(autouse=True)
def clear_events_cache():
    collector._events_cache.clear()
    yield
    collector._events_cache.clear()


class TestOutcomeMapping:

    def test_up_down_outcomes_map_to_yes_no_with_labels(self):
        parsed = parse_market_dict(_market("0xbtc", "Bitcoin Up or Down - 10:40AM-10:45AM ET",
                                           outcomes='["Up", "Down"]', prices='["0.62", "0.38"]'))
        assert parsed["price_yes"] == Decimal("0.62")
        assert parsed["price_no"] == Decimal("0.38")
        assert parsed["outcome_yes_label"] == "Up"
        assert parsed["outcome_no_label"] == "Down"

    def test_inverted_down_up_order(self):
        parsed = parse_market_dict(_market("0xbtc", "BTC?", outcomes='["Down", "Up"]', prices='["0.9", "0.1"]'))
        assert parsed["price_yes"] == Decimal("0.1")
        assert parsed["price_no"] == Decimal("0.9")

    def test_yes_no_labels_default(self):
        parsed = parse_market_dict(_market("0xa", "Q?"))
        assert (parsed["outcome_yes_label"], parsed["outcome_no_label"]) == ("Yes", "No")

    def test_up_down_resolution(self):
        raw = _market("0xbtc", "BTC?", outcomes='["Up", "Down"]', prices='["0", "1"]',
                      closed=True, umaResolutionStatus="resolved")
        assert determine_winning_outcome(raw) == "NO"

    def test_labels_are_persisted_and_returned(self):
        now = datetime.now(timezone.utc)
        db = get_db_session()
        db.add(MarketSnapshot(id=uuid.uuid4(), market_id="0xbtc", market_name="BTC Up or Down", status="open",
                              is_resolved=False, price_yes=Decimal("0.5"), price_no=Decimal("0.5"),
                              outcome_yes_label="Up", outcome_no_label="Down", timestamp=now))
        db.commit()
        db.close()
        market = get_market_by_id("0xbtc")
        assert (market["outcome_yes_label"], market["outcome_no_label"]) == ("Up", "Down")


class TestCategoryRegistry:

    def test_default_enabled_categories(self):
        assert [c.key for c in enabled_categories()] == ["weather", "elon_tweets"]

    def test_categories_can_be_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "ENABLED_MARKET_CATEGORIES", "weather")
        assert [c.key for c in enabled_categories()] == ["weather"]

    def test_unknown_category_raises(self):
        with pytest.raises(KeyError):
            get_category("does_not_exist")


class TestElonTweetsCollection:

    def test_elon_markets_filtered_by_title_and_labelled(self):
        with patch("urllib.request.urlopen", side_effect=_fake_gamma):
            markets = fetch_category_markets(get_category("elon_tweets"))
        ids = {m["market_id"] for m in markets}
        assert ids == {"0xelon_a", "0xelon_b"}  # event Trump di tag yang sama diabaikan
        assert {m["category"] for m in markets} == {"Elon Tweets"}

    def test_fetch_all_markets_combines_enabled_categories(self):
        with patch("urllib.request.urlopen", side_effect=_fake_gamma):
            ids = {m["market_id"] for m in fetch_all_markets()}
        assert ids == {"0xnyc", "0xelon_a", "0xelon_b"}

    def test_fetch_all_markets_respects_disabled_category(self, monkeypatch):
        monkeypatch.setattr(settings, "ENABLED_MARKET_CATEGORIES", "weather")
        with patch("urllib.request.urlopen", side_effect=_fake_gamma):
            ids = {m["market_id"] for m in fetch_all_markets()}
        assert ids == {"0xnyc"}

    def test_grouped_elon_events(self):
        with patch("urllib.request.urlopen", side_effect=_fake_gamma):
            events = fetch_category_events("elon_tweets")
        assert [e["event_id"] for e in events] == ["ev-elon"]
        event = events[0]
        assert event["category"] == "elon_tweets"
        assert event["end_date"] == "2026-09-29"
        # bracket dengan peluang tertinggi lebih dulu
        assert [m["group_item_title"] for m in event["markets"]] == ["220-239", "200-219"]
        assert event["markets"][0]["outcome_yes_label"] == "Yes"

    def test_events_cache_is_per_category(self):
        with patch("urllib.request.urlopen", side_effect=_fake_gamma):
            weather = fetch_category_events("weather")
            elon = fetch_category_events("elon_tweets")
        assert {e["event_id"] for e in weather} == {"ev-weather"}
        assert {e["event_id"] for e in elon} == {"ev-elon"}

    def test_paper_order_on_elon_bracket(self):
        now = datetime.now(timezone.utc)
        db = get_db_session()
        db.add(MarketSnapshot(id=uuid.uuid4(), market_id="0xelon_b", market_name="Will Elon Musk post 220-239 tweets?",
                              status="open", is_resolved=False, resolution_time=now + timedelta(days=3),
                              price_yes=Decimal("0.40"), price_no=Decimal("0.60"), category="Elon Tweets",
                              timestamp=now))
        db.commit()
        db.close()
        order = create_paper_order("0xelon_b", "YES", Decimal("1.00"))
        assert order["status"] == "FILLED"
        assert order["shares"] == Decimal("2.5000")


class TestCategoryApi:

    def test_categories_endpoint(self):
        from app.dashboard import app
        res = TestClient(app).get("/api/markets/categories")
        assert res.json() == [{"key": "weather", "label": "Cuaca"}, {"key": "elon_tweets", "label": "Elon Musk Tweets"}]

    def test_events_endpoint_for_elon(self):
        from app.dashboard import app
        with patch("urllib.request.urlopen", side_effect=_fake_gamma):
            res = TestClient(app).get("/api/markets/events", params={"category": "elon_tweets"})
        assert res.status_code == 200
        assert res.json()[0]["event_id"] == "ev-elon"

    def test_disabled_category_returns_404(self, monkeypatch):
        from app.dashboard import app
        monkeypatch.setattr(settings, "ENABLED_MARKET_CATEGORIES", "weather")
        assert TestClient(app).get("/api/markets/events", params={"category": "elon_tweets"}).status_code == 404

    def test_weather_events_alias_still_works(self):
        from app.dashboard import app
        with patch("urllib.request.urlopen", side_effect=_fake_gamma):
            res = TestClient(app).get("/api/markets/weather-events")
        assert res.status_code == 200
        assert {e["event_id"] for e in res.json()} == {"ev-weather"}
