"""A restart or a failover must not blank the chart or restart the candle that is being built."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.candles import CandleAggregator
from src.models import Candle, PriceTick
from src.timeutil import get_zone, now_ist, today_ist

IST = get_zone("Asia/Kolkata")


def make_candle(start, interval=1, o=100.0, c=101.0, ticks=4, complete=True):
    return Candle(symbol="NIFTY", interval_minutes=interval, open_time=start, close_time=start + timedelta(minutes=interval),
                  open=o, high=max(o, c) + 1, low=min(o, c) - 1, close=c, tick_count=ticks, is_complete=complete)


def tick(ts, price):
    return PriceTick(symbol="NIFTY", price=price, timestamp=ts)


# -- aggregator ------------------------------------------------------------------------


def test_restore_continues_the_in_progress_candle():
    agg = CandleAggregator("NIFTY", [1], session_start="09:15")
    start = datetime(2026, 10, 15, 10, 30, tzinfo=IST)
    agg.restore(active={1: make_candle(start, o=100.0, c=101.0, ticks=4, complete=False)})
    done = agg.process_tick(tick(start + timedelta(seconds=40), 105.0))
    assert done == []  # same minute: still building
    live = agg.active_candles[1]
    assert (live.open, live.high, live.close, live.tick_count) == (100.0, 105.0, 105.0, 5)  # open kept, not reset to 105


def test_a_restored_candle_that_was_overtaken_is_finalised_by_the_next_tick():
    agg = CandleAggregator("NIFTY", [1], session_start="09:15")
    start = datetime(2026, 10, 15, 10, 30, tzinfo=IST)
    agg.restore(active={1: make_candle(start, complete=False)})
    done = agg.process_tick(tick(start + timedelta(minutes=3), 110.0))  # leader was down for 3 minutes
    assert [c.open_time for c in done] == [start]
    assert done[0].is_complete


def test_restore_fills_completed_history_only_when_empty():
    agg = CandleAggregator("NIFTY", [1], session_start="09:15")
    base = datetime(2026, 10, 15, 9, 15, tzinfo=IST)
    history = [make_candle(base + timedelta(minutes=i)) for i in (2, 0, 1)]  # deliberately unordered
    agg.restore(completed={1: history})
    assert [c.open_time for c in agg.completed_candles[1]] == [base, base + timedelta(minutes=1), base + timedelta(minutes=2)]
    agg.restore(completed={1: [make_candle(base + timedelta(minutes=9))]})  # a second call must not overwrite
    assert len(agg.completed_candles[1]) == 3


def test_restore_never_replaces_live_state():
    agg = CandleAggregator("NIFTY", [1], session_start="09:15")
    start = datetime(2026, 10, 15, 10, 30, tzinfo=IST)
    agg.process_tick(tick(start, 100.0))
    agg.restore(active={1: make_candle(start, o=999.0, complete=False)})
    assert agg.active_candles[1].open == 100.0


def test_restore_ignores_intervals_the_aggregator_does_not_build():
    agg = CandleAggregator("NIFTY", [1], session_start="09:15")
    start = datetime(2026, 10, 15, 10, 30, tzinfo=IST)
    agg.restore(active={5: make_candle(start, interval=5, complete=False)}, completed={5: [make_candle(start, interval=5)]})
    assert agg.active_candles == {}
    assert set(agg.completed_candles) == {1}


# -- engine ------------------------------------------------------------------------------


def engine_config(db_path):
    return {
        "underlying": "NIFTY",
        "data_source": "mock",
        "poll_interval_seconds": 5,
        "candle_intervals": [1],
        "price_calculator": "mock",
        "persistence": {"enabled": True, "db_path": str(db_path), "option_chain_snapshot_interval_seconds": 60},
    }


@pytest.fixture
def engine(tmp_path):
    from src.engine import MarketEngine

    e = MarketEngine(engine_config(tmp_path / "e.db"))
    yield e
    e.close()


def test_engine_restores_todays_completed_candles_from_history(engine):
    midnight = datetime.combine(today_ist(), datetime.min.time(), tzinfo=IST)
    yesterday = midnight - timedelta(days=1)
    for i in range(3):
        engine._persistence.save_candle(make_candle(midnight + timedelta(minutes=i)))
    engine._persistence.save_candle(make_candle(yesterday))  # another day: must not appear
    restored = engine.restore_candle_state()
    assert restored == {"completed": 3, "active": 0}
    assert len(engine._aggregator.completed_candles[1]) == 3


def test_engine_continues_the_previous_leaders_active_candle(engine):
    start = now_ist().replace(second=0, microsecond=0)
    payload = {"symbol": "NIFTY", "interval_minutes": 1, "open_time": start.isoformat(), "close_time": (start + timedelta(minutes=1)).isoformat(),
               "open": 24800.0, "high": 24810.0, "low": 24795.0, "close": 24805.0, "tick_count": 7, "is_complete": False}
    restored = engine.restore_candle_state(active={1: payload})
    assert restored["active"] == 1
    assert engine._aggregator.active_candles[1].open == 24800.0
    assert engine._aggregator.active_candles[1].tick_count == 7


def test_an_active_candle_already_stored_as_completed_is_not_carried_over_twice(engine):
    start = datetime.combine(today_ist(), datetime.min.time(), tzinfo=IST) + timedelta(minutes=5)
    engine._persistence.save_candle(make_candle(start))
    payload = {"symbol": "NIFTY", "interval_minutes": 1, "open_time": start.isoformat(), "close_time": (start + timedelta(minutes=1)).isoformat(),
               "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "tick_count": 1}
    assert engine.restore_candle_state(active={1: payload})["active"] == 0


def test_restore_runs_once_and_survives_garbage(engine):
    bad = {"open_time": "not a date"}
    assert engine.restore_candle_state(active={1: bad, 5: "junk"})["active"] == 0
    assert engine.restore_candle_state() == {"completed": 0, "active": 0}  # second call is a no-op


def test_start_restores_history_automatically(tmp_path):
    from src.engine import MarketEngine

    cfg = engine_config(tmp_path / "auto.db")
    first = MarketEngine(cfg)
    midnight = datetime.combine(today_ist(), datetime.min.time(), tzinfo=IST)
    first._persistence.save_candle(make_candle(midnight))
    first.close()

    second = MarketEngine(cfg)  # a process restart
    try:
        second.start()
        assert len(second._aggregator.completed_candles[1]) >= 1
    finally:
        second.close()


# -- backend choice ----------------------------------------------------------------------


def test_history_backend_selection(monkeypatch):
    from src.engine import _history_backend

    for var in ("MARKET_HISTORY_BACKEND", "AUTH_MODE", "DATABASE_URL", "APP_ENV"):
        monkeypatch.delenv(var, raising=False)
    assert _history_backend({}) == "sqlite"  # local development keeps its file
    monkeypatch.setenv("AUTH_MODE", "required")
    assert _history_backend({}) == "sqlite"  # signed-in but no shared database configured
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    assert _history_backend({}) == "sql"  # production shape: durable shared history
    assert _history_backend({"backend": "sqlite"}) == "sqlite"
    monkeypatch.setenv("MARKET_HISTORY_BACKEND", "sqlite")
    assert _history_backend({"backend": "sql"}) == "sqlite"  # the environment wins over the file
    monkeypatch.setenv("MARKET_HISTORY_BACKEND", "nonsense")
    assert _history_backend({}) == "sql"  # unknown values fall back to the default rule


# -- ingestor carry-over -----------------------------------------------------------------


def test_ingestor_reads_the_previous_leaders_active_candles_from_redis():
    import json

    from src.ingestor import Ingestor

    class FakeRedis:
        def __init__(self, store):
            self.store = store

        def get(self, key):
            return self.store.get(key)

    def message(topic, payload):
        return json.dumps({"t": topic, "e": 5, "s": 9, "ts": 1, "pub": 2, "d": payload})

    ing = Ingestor.__new__(Ingestor)
    ing.prefix = "rt:"

    class Eng:
        underlying = "NIFTY"
        intervals = [1, 5, 15]

    ing.engine = Eng()
    active = {"symbol": "NIFTY", "interval_minutes": 1, "open_time": "2026-10-15T10:30:00+05:30"}
    ing.client = FakeRedis({
        "rt:snap:candles:NIFTY:1m": message("candles:NIFTY:1m", {"active": active, "series": []}),
        "rt:snap:candles:NIFTY:5m": "garbage that is not json",
        # 15m: nothing published
    })
    assert ing._previous_active_candles() == {1: active}
