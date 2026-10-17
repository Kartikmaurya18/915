"""
Engine-level tests for the historical Date/Session selector: MarketEngine
wiring around PersistenceStore for trading-date listing and the read-only
historical session bundle (candles, option chain, straddle candles, paper
trading history/P&L) — see MarketEngine.get_available_trading_dates() /
get_historical_session() in src/engine.py.
"""

import time
from datetime import datetime

import pytest

from src.engine import MarketEngine
from src.models import Candle
from src.market_calendar import is_trading_day as is_nse_trading_day
from src.timeutil import IST, today_ist


def make_engine(tmp_path, **overrides):
    cfg = {
        "underlying": "NIFTY",
        "data_source": "mock",
        "poll_interval_seconds": 0.05,
        "candle_intervals": [1, 5, 15],
        "price_calculator": "mock",
        "persistence": {
            "enabled": True,
            "db_path": str(tmp_path / "engine.db"),
            "option_chain_snapshot_interval_seconds": 0.05,
        },
    }
    cfg.update(overrides)
    return MarketEngine(cfg)


def test_available_trading_dates_includes_today_after_live_ticks(tmp_path):
    engine = make_engine(tmp_path)
    engine.start()
    try:
        time.sleep(0.6)
        engine.stop()  # halt live ticking, but keep the persistence handle open for reads
        result = engine.get_available_trading_dates()
    finally:
        engine.close()

    assert result["today"] == today_ist().isoformat()
    # Persisted dates are filtered through the NSE calendar: a weekday with data is listed, but a weekend or
    # holiday is not offered even when ticks were stored for it (the test used to assume a trading day).
    assert (today_ist().isoformat() in result["dates"]) == is_nse_trading_day(today_ist())


def test_historical_session_for_todays_date_has_data_after_live_ticks(tmp_path):
    engine = make_engine(tmp_path)
    engine.start()
    try:
        time.sleep(0.6)
        engine.stop()  # halt live ticking, but keep the persistence handle open for reads
        today = today_ist().isoformat()
        session = engine.get_historical_session(today)
    finally:
        engine.close()

    assert session["has_data"] is True
    assert session["trading_date"] == today
    assert "1" in session["candles"]
    assert len(session["option_chain_snapshots"]) > 0
    assert session["underlying"]["symbol"] == "NIFTY"
    assert session["underlying"]["close"] is not None
    # Straddle candles auto-focus on the last snapshot's ATM strike when none
    # is requested. `available_strikes` is populated from *completed*
    # per-strike candles (see StrikeStraddleCandleEngine), which need a full
    # interval boundary to close — in this short test window that can still
    # be empty even though the focus strike itself resolves fine from the
    # option chain snapshots.
    assert session["straddle_candles"] is not None
    assert isinstance(session["available_strikes"], list)
    # Paper trading shape matches PaperTradingEngine.to_state_dict()'s
    # closed-book fields, reused by the same frontend renderers.
    assert "history" in session["paper_trading"]
    assert "stats" in session["paper_trading"]
    assert "pnl_summary" in session["paper_trading"]


def test_historical_session_for_a_date_with_no_data_reports_has_data_false(tmp_path):
    engine = make_engine(tmp_path)
    engine.start()
    try:
        time.sleep(0.3)
        engine.stop()
        session = engine.get_historical_session("2020-01-01")
    finally:
        engine.close()

    assert session["has_data"] is False
    assert session["candles"] == {} or all(v == [] for v in session["candles"].values())
    assert session["option_chain_snapshots"] == []
    assert session["paper_trading"]["history"] == []
    assert session["paper_trading"]["stats"]["total_trades"] == 0


def test_historical_session_read_only_snapshot_does_not_move_with_new_ticks(tmp_path):
    """Reading the same past date twice, with live ticks happening for
    *today* in between, must return the exact same persisted bars — a
    historical session is a frozen read, never a live one."""
    engine = make_engine(tmp_path)
    engine.start()
    try:
        time.sleep(0.4)
        today = today_ist().isoformat()
        first = engine.get_historical_session(today)
        first_candle_count = len(first["candles"]["1"])

        time.sleep(0.4)  # more live ticks land for "today" in the live pipeline

        # A snapshot taken from the *live* pipeline keeps moving...
        live_snapshot = engine.get_snapshot()
        assert live_snapshot["tick_count"] > 0

        # ...but re-reading the identical already-completed historical bars
        # must not retroactively change (only newly *completed* bars, if
        # any, are appended — nothing already returned gets rewritten).
        second = engine.get_historical_session(today)
        assert second["candles"]["1"][:first_candle_count] == first["candles"]["1"]
    finally:
        engine.close()


def test_available_dates_excludes_a_weekend_or_holiday_even_if_data_exists(tmp_path):
    """The Date/Session selector must never offer a weekend/NSE-holiday
    date, even if some MOCK data happens to be persisted under it (e.g. a
    stray dev/test run) — see market_calendar.is_trading_day and its use in
    get_available_trading_dates()/get_historical_session()."""
    engine = make_engine(tmp_path)
    store = engine._persistence
    # 2026-09-12/13 = Sat/Sun; 2026-09-14 = Ganesh Chaturthi (a Monday NSE
    # holiday, per data/nse_holidays.json) — all three get MOCK candles
    # written directly, bypassing the live pipeline's own calendar.
    for day in (12, 13, 14):
        store.save_candle(
            Candle(
                symbol="NIFTY",
                interval_minutes=1,
                open_time=datetime(2026, 9, day, 10, 0, 0, tzinfo=IST),
                close_time=datetime(2026, 9, day, 10, 1, 0, tzinfo=IST),
                open=100.0,
                high=110.0,
                low=95.0,
                close=105.0,
                tick_count=5,
                is_complete=True,
            )
        )

    dates = engine.get_available_trading_dates()["dates"]
    assert "2026-09-12" not in dates
    assert "2026-09-13" not in dates
    assert "2026-09-14" not in dates

    for day in ("2026-09-12", "2026-09-13", "2026-09-14"):
        session = engine.get_historical_session(day)
        assert session["has_data"] is False, f"{day} should report no data (weekend/holiday)"

    engine.close()


def test_persistence_disabled_returns_empty_dates_and_no_data_session(tmp_path):
    engine = make_engine(tmp_path, persistence={"enabled": False})
    result = engine.get_available_trading_dates()
    assert result["dates"] == []

    session = engine.get_historical_session("2026-01-01")
    assert session["has_data"] is False
    assert session["paper_trading"]["stats"]["total_trades"] == 0
