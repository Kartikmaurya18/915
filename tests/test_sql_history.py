"""The shared-database history store must answer every question exactly like the SQLite store it replaces.

Both stores receive identical writes; every read method is then compared. Runs on SQLite, and on
PostgreSQL as well when TEST_DATABASE_URL is set.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.models import ATMResult, Candle, OptionChainSnapshot
from src.paper_trading import PaperPosition
from src.persistence import PersistenceStore
from src.timeutil import get_zone

IST = get_zone("Asia/Kolkata")


@pytest.fixture
def stores(tmp_path, monkeypatch):
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
    old, new = PersistenceStore(tmp_path / "old.db"), SqlPersistenceStore()
    yield old, new
    old.close()
    new.close()
    db_session.reset_engine()


def candle(symbol, interval, start, price, ticks=3):
    return Candle(symbol=symbol, interval_minutes=interval, open_time=start, close_time=start + timedelta(minutes=interval),
                  open=price, high=price + 2, low=price - 1, close=price + 1, tick_count=ticks, is_complete=True)


def chain(ts, ltp=24800.0):
    return OptionChainSnapshot(underlying="NIFTY", underlying_ltp=ltp, expiry="22-Oct-2026", timestamp=ts, strikes=[])


def atm(strike=24800.0):
    return ATMResult(strike=strike, call_ltp=100.5, put_ltp=99.5, straddle_premium=200.0, underlying_ltp=24801.0, distance_from_spot=1.0)


def trade(pid, entry):
    return PaperPosition(id=pid, option_type="CE", strike=24800.0, quantity=50, entry_price=100.0, entry_time=entry,
                         take_profit_percent=10.0, stop_loss_percent=5.0, side="BUY", exit_price=110.0,
                         exit_time=entry + timedelta(minutes=5), exit_reason="TAKE_PROFIT", pnl=500.0, pnl_percent=10.0, status="EXITED")


@pytest.fixture
def filled(stores):
    old, new = stores
    day1 = datetime(2026, 10, 14, 9, 15, tzinfo=IST)
    day2 = datetime(2026, 10, 15, 9, 15, tzinfo=IST)
    for store in (old, new):
        for day in (day1, day2):
            for i in range(4):
                start = day + timedelta(minutes=i)
                store.save_candle(candle("NIFTY", 1, start, 24800 + i))
                store.save_candle(candle("24800.0_ce", 1, start, 100 + i))
                store.save_candle(candle("24800.0_pe", 1, start, 90 + i))
                store.save_candle(candle("24800.0_straddle", 1, start, 190 + i))
            store.save_candle(candle("NIFTY", 5, day, 24800))
            store.save_candle(candle("24850.0_ce", 1, day, 50))
            store.save_option_chain_snapshot(chain(day + timedelta(seconds=1)), atm())
            store.save_option_chain_snapshot(chain(day + timedelta(seconds=6)), atm(24850.0))
            store.save_option_chain_snapshot(chain(day + timedelta(seconds=11)), None)  # no ATM resolved
        store.save_paper_trade(trade("t1", day1 + timedelta(hours=1)))
        store.save_paper_trade(trade("t2", day2 + timedelta(hours=2)))
        store.save_candle(candle("NIFTY", 1, day1, 24900))  # re-save: upsert, not a duplicate
    return old, new


def test_recent_reads_match(filled):
    old, new = filled
    assert new.get_recent_candles("NIFTY", 1, 5) == old.get_recent_candles("NIFTY", 1, 5)
    assert new.get_recent_candles("NIFTY", 5, 10) == old.get_recent_candles("NIFTY", 5, 10)
    assert new.get_recent_option_chain_snapshots("NIFTY", 4) == old.get_recent_option_chain_snapshots("NIFTY", 4)


def test_date_reads_match(filled):
    old, new = filled
    assert new.list_trading_dates() == old.list_trading_dates() == ["2026-10-15", "2026-10-14"]
    assert new.list_trading_dates("NIFTY") == old.list_trading_dates("NIFTY")
    assert new.list_trading_dates("OTHER") == old.list_trading_dates("OTHER")  # candles/trades still make the day exist
    for day in ("2026-10-14", "2026-10-15", "2026-01-01"):
        assert new.get_candles_for_date("NIFTY", 1, day) == old.get_candles_for_date("NIFTY", 1, day)
        assert new.get_option_chain_snapshots_for_date("NIFTY", day) == old.get_option_chain_snapshots_for_date("NIFTY", day)
        assert new.list_strikes_for_date(day) == old.list_strikes_for_date(day)
        assert new.get_paper_trades_for_date(day) == old.get_paper_trades_for_date(day)


def test_strike_candles_match_and_strikes_are_found(filled):
    old, new = filled
    assert new.list_strikes_for_date("2026-10-15") == [24800.0, 24850.0]
    assert new.get_strike_candles_for_date(24800.0, [1, 5], "2026-10-15") == old.get_strike_candles_for_date(24800.0, [1, 5], "2026-10-15")


def test_resaving_a_candle_updates_it(filled):
    _, new = filled
    first = new.get_candles_for_date("NIFTY", 1, "2026-10-14")[0]
    assert first["open"] == 24900  # the re-save replaced the original 24800
    assert len(new.get_candles_for_date("NIFTY", 1, "2026-10-14")) == 4


def test_limits_apply(filled):
    old, new = filled
    assert new.get_candles_for_date("NIFTY", 1, "2026-10-15", limit=2) == old.get_candles_for_date("NIFTY", 1, "2026-10-15", limit=2)
    assert len(new.get_option_chain_snapshots_for_date("NIFTY", "2026-10-15", limit=1)) == 1


def test_purge_removes_only_older_days(filled):
    _, new = filled
    before = new.count_candles()
    removed = new.purge_before("2026-10-15")
    assert removed > 0
    assert new.count_candles() < before
    assert new.get_candles_for_date("NIFTY", 1, "2026-10-14") == []
    assert len(new.get_candles_for_date("NIFTY", 1, "2026-10-15")) == 4
    assert new.get_recent_option_chain_snapshots("NIFTY", 100)  # today's snapshots survive


def test_a_naive_looking_utc_time_is_stored_as_ist(stores):
    """Candles built from UTC timestamps are stored and dated in India time, like the SQLite store."""
    old, new = stores
    start = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)  # 01:30 IST on the 15th
    for store in (old, new):
        store.save_candle(candle("NIFTY", 1, start, 100))
    assert new.get_candles_for_date("NIFTY", 1, "2026-10-15") == old.get_candles_for_date("NIFTY", 1, "2026-10-15")
    assert len(new.get_candles_for_date("NIFTY", 1, "2026-10-15")) == 1


def test_engine_writes_history_to_the_shared_database(stores, monkeypatch):
    """With MARKET_HISTORY_BACKEND=sql the running engine records into the database, readable by any process."""
    import time

    from src.engine import MarketEngine

    _, new = stores
    monkeypatch.setenv("MARKET_HISTORY_BACKEND", "sql")
    cfg = {
        "underlying": "NIFTY", "data_source": "mock", "poll_interval_seconds": 0.05, "candle_intervals": [1],
        "price_calculator": "mock",
        "persistence": {"enabled": True, "db_path": "unused.db", "option_chain_snapshot_interval_seconds": 0.1},
    }
    engine = MarketEngine(cfg)
    assert type(engine._persistence).__name__ == "SqlPersistenceStore"
    engine.start()
    try:
        time.sleep(0.8)
    finally:
        engine.close()
    assert len(new.get_recent_option_chain_snapshots("NIFTY", 50)) > 0  # a separate store object sees the engine's writes
    assert new.list_trading_dates("NIFTY")
