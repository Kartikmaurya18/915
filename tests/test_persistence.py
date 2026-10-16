"""Tests for the optional SQLite persistence layer (candles + option chain snapshots)."""

from datetime import datetime

import pytest

from src.models import Candle, OptionChainSnapshot, OptionLeg, ATMResult
from src.persistence import PersistenceStore


def make_candle(open_time, close_time, o=100.0, h=110.0, l=95.0, c=105.0, ticks=5, interval=1):
    return Candle(
        symbol="NIFTY",
        interval_minutes=interval,
        open_time=open_time,
        close_time=close_time,
        open=o,
        high=h,
        low=l,
        close=c,
        tick_count=ticks,
        is_complete=True,
    )


@pytest.fixture(params=["sqlite", "sql"])
def store(request, tmp_path, monkeypatch):
    """Every behaviour below must hold for BOTH backends: the original SQLite file store and the
    shared-database store (SQLite here; PostgreSQL when TEST_DATABASE_URL is set)."""
    if request.param == "sqlite":
        s = PersistenceStore(tmp_path / "test.db")
        yield s
        s.close()
        return

    from src.db import session as db_session
    from src.db.history import SqlPersistenceStore
    from src.db.migrate import downgrade_to_base, upgrade_to_head
    from tests.conftest import external_test_database_url

    external = external_test_database_url()
    monkeypatch.setenv("DATABASE_URL", external or f"sqlite:///{(tmp_path / 'sql.db').as_posix()}")
    db_session.reset_engine()
    if external:
        downgrade_to_base()
    upgrade_to_head()
    s = SqlPersistenceStore()
    yield s
    s.close()
    db_session.reset_engine()


def test_save_and_read_back_candle(store):
    candle = make_candle(datetime(2026, 8, 23, 10, 30, 0), datetime(2026, 8, 23, 10, 31, 0))
    store.save_candle(candle)

    rows = store.get_recent_candles("NIFTY", 1, limit=10)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "NIFTY"
    assert rows[0]["interval_minutes"] == 1
    assert rows[0]["open"] == 100.0
    assert rows[0]["high"] == 110.0
    assert rows[0]["low"] == 95.0
    assert rows[0]["close"] == 105.0
    assert rows[0]["tick_count"] == 5


def test_candles_ordered_oldest_to_newest(store):
    base = datetime(2026, 8, 23, 10, 30, 0)
    for i in range(3):
        store.save_candle(
            make_candle(
                base.replace(minute=30 + i), base.replace(minute=31 + i), o=100.0 + i
            )
        )

    rows = store.get_recent_candles("NIFTY", 1, limit=10)
    assert [r["open"] for r in rows] == [100.0, 101.0, 102.0]


def test_save_candle_is_idempotent_upsert(store):
    """Re-saving a candle for the same open_time (e.g. a late correction) updates, not duplicates."""
    open_time = datetime(2026, 8, 23, 10, 30, 0)
    close_time = datetime(2026, 8, 23, 10, 31, 0)
    store.save_candle(make_candle(open_time, close_time, c=105.0, ticks=5))
    store.save_candle(make_candle(open_time, close_time, c=107.0, ticks=6))

    rows = store.get_recent_candles("NIFTY", 1, limit=10)
    assert len(rows) == 1
    assert rows[0]["close"] == 107.0
    assert rows[0]["tick_count"] == 6


def test_different_intervals_kept_separate(store):
    base_open = datetime(2026, 8, 23, 10, 30, 0)
    store.save_candle(make_candle(base_open, base_open, interval=1))
    store.save_candle(make_candle(base_open, base_open, interval=5))

    assert len(store.get_recent_candles("NIFTY", 1, limit=10)) == 1
    assert len(store.get_recent_candles("NIFTY", 5, limit=10)) == 1
    assert len(store.get_recent_candles("NIFTY", 15, limit=10)) == 0


def test_save_and_read_back_option_chain_snapshot(store):
    chain = OptionChainSnapshot(
        underlying="NIFTY",
        underlying_ltp=25000.0,
        expiry="28-Aug-2026",
        timestamp=datetime(2026, 8, 23, 10, 30, 0),
        strikes=[OptionLeg(strike=25000.0, call_ltp=150.0, put_ltp=140.0)],
    )
    atm = ATMResult(
        strike=25000.0,
        call_ltp=150.0,
        put_ltp=140.0,
        straddle_premium=290.0,
        underlying_ltp=25000.0,
        distance_from_spot=0.0,
    )
    store.save_option_chain_snapshot(chain, atm)

    rows = store.get_recent_option_chain_snapshots("NIFTY", limit=10)
    assert len(rows) == 1
    assert rows[0]["underlying_ltp"] == 25000.0
    assert rows[0]["atm_strike"] == 25000.0
    assert rows[0]["atm_call_ltp"] == 150.0
    assert rows[0]["atm_put_ltp"] == 140.0
    assert rows[0]["straddle_premium"] == 290.0


def test_option_chain_snapshot_without_atm(store):
    chain = OptionChainSnapshot(
        underlying="NIFTY",
        underlying_ltp=25000.0,
        expiry="28-Aug-2026",
        timestamp=datetime(2026, 8, 23, 10, 30, 0),
        strikes=[],
    )
    store.save_option_chain_snapshot(chain, atm=None)

    rows = store.get_recent_option_chain_snapshots("NIFTY", limit=10)
    assert len(rows) == 1
    assert rows[0]["atm_strike"] is None


def test_empty_store_returns_empty_list(store):
    assert store.get_recent_candles("NIFTY", 1) == []
    assert store.get_recent_option_chain_snapshots("NIFTY") == []


def test_survives_reopen_at_same_path(tmp_path):
    """Data written by one PersistenceStore instance is visible after reopening the file."""
    path = tmp_path / "reopen.db"
    s1 = PersistenceStore(path)
    s1.save_candle(make_candle(datetime(2026, 8, 23, 10, 30, 0), datetime(2026, 8, 23, 10, 31, 0)))
    s1.close()

    s2 = PersistenceStore(path)
    rows = s2.get_recent_candles("NIFTY", 1, limit=10)
    assert len(rows) == 1
    s2.close()


def test_engine_persists_completed_candles_end_to_end(tmp_path):
    """MarketEngine, with persistence enabled, writes completed candles to the DB."""
    import time
    from src.engine import MarketEngine

    db_path = tmp_path / "engine.db"
    cfg = {
        "underlying": "NIFTY",
        "data_source": "mock",
        "poll_interval_seconds": 0.05,
        "candle_intervals": [1],
        "price_calculator": "mock",
        "persistence": {
            "enabled": True,
            "db_path": str(db_path),
            "option_chain_snapshot_interval_seconds": 0.1,
        },
    }
    engine = MarketEngine(cfg)
    engine.start()
    try:
        time.sleep(0.5)
        snapshots = engine.get_persisted_option_chain_snapshots(limit=10)
        assert len(snapshots) > 0, "Option chain snapshots should be persisted"
    finally:
        engine.close()

    # Reopen independently to confirm durability past the engine's own connection.
    store = PersistenceStore(db_path)
    try:
        assert len(store.get_recent_option_chain_snapshots("NIFTY", limit=10)) > 0
    finally:
        store.close()
