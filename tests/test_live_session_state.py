"""What the engine reports about a real feed: session by the wall clock, option-chain age, credential expiry."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.engine import MarketEngine
from src.timeutil import IST

CFG = {
    "underlying": "NIFTY",
    "market_data_provider": "mock",
    "data_source": "mock",
    "poll_interval_seconds": 5,
    "candle_intervals": [1],
    "price_calculator": "mock",
    "persistence": {"enabled": False},
    "log_level": "ERROR",
}

WEDNESDAY_11 = datetime(2026, 10, 21, 11, 0, tzinfo=IST)
WEDNESDAY_1545 = datetime(2026, 10, 21, 15, 45, tzinfo=IST)
SATURDAY_11 = datetime(2026, 10, 17, 11, 0, tzinfo=IST)


@pytest.fixture
def engine():
    e = MarketEngine(dict(CFG))
    e.tick_once()
    yield e
    e.close()


def live(engine, monkeypatch, now: datetime, expires: datetime | None = None):
    """Make the engine believe its feed is a real one and the wall clock is `now`."""
    engine._source.is_simulated = False
    engine._source.credentials_expire_at = lambda: expires
    monkeypatch.setattr("src.engine.now_ist", lambda tz=None: now)
    return engine.get_snapshot()["data_quality"], engine.get_snapshot()["provider"]


def age_the_data(engine, seconds: float, now: datetime):
    stamp = now - timedelta(seconds=seconds)
    engine._latest_tick.timestamp = stamp
    engine._latest_chain.timestamp = stamp


def test_a_closed_market_is_reported_as_closed_not_as_a_broken_feed(engine, monkeypatch):
    age_the_data(engine, 3600, WEDNESDAY_1545)
    quality, _ = live(engine, monkeypatch, WEDNESDAY_1545)
    assert quality["market_session"] == "CLOSED" and quality["market_open"] is False
    assert quality["is_stale"] is False  # silence after the close is expected


def test_a_weekend_is_reported_as_one(engine, monkeypatch):
    age_the_data(engine, 3600, SATURDAY_11)
    quality, _ = live(engine, monkeypatch, SATURDAY_11)
    assert quality["market_session"] == "WEEKEND" and quality["market_open"] is False and quality["is_stale"] is False


def test_during_the_session_silence_is_stale(engine, monkeypatch):
    age_the_data(engine, 120, WEDNESDAY_11)
    quality, _ = live(engine, monkeypatch, WEDNESDAY_11)
    assert quality["market_session"] == "OPEN" and quality["market_open"] is True
    assert quality["is_stale"] is True
    assert quality["last_tick_age_seconds"] == pytest.approx(120, abs=1)


def test_a_fresh_tick_during_the_session_is_not_stale(engine, monkeypatch):
    age_the_data(engine, 1, WEDNESDAY_11)
    quality, _ = live(engine, monkeypatch, WEDNESDAY_11)
    assert quality["is_stale"] is False


def test_the_chain_ages_independently_of_the_index_tick(engine, monkeypatch):
    """The index can keep ticking while the chain behind it has stopped refreshing."""
    engine._latest_tick.timestamp = WEDNESDAY_11 - timedelta(seconds=1)
    engine._latest_chain.timestamp = WEDNESDAY_11 - timedelta(seconds=95)
    quality, _ = live(engine, monkeypatch, WEDNESDAY_11)
    assert quality["is_stale"] is False
    assert quality["chain_age_seconds"] == pytest.approx(95, abs=1)


def test_a_simulated_feed_keeps_the_session_of_its_latest_tick(engine, monkeypatch):
    monkeypatch.setattr("src.engine.now_ist", lambda tz=None: SATURDAY_11)
    quality = engine.get_snapshot()["data_quality"]
    assert quality["market_session"] == engine._latest_market_session  # mock ticks around the clock, by design


def test_credential_expiry_is_published_for_the_ui_to_warn_about(engine, monkeypatch):
    expires = WEDNESDAY_11 + timedelta(minutes=40)
    _, provider = live(engine, monkeypatch, WEDNESDAY_11, expires=expires)
    assert provider["credentials_expire_at"] == expires.isoformat()
    _, provider = live(engine, monkeypatch, WEDNESDAY_11, expires=None)
    assert provider["credentials_expire_at"] is None
