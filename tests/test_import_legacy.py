"""Importing the original SQLite file into the shared database."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from src.db import session as db_session
from src.db.import_legacy import import_history, import_paper_trades, main
from src.db.migrate import downgrade_to_base, upgrade_to_head
from src.db.models import MarketCandle, MarketChainSnapshot, PaperPositionRow, User
from src.db.history import SqlPersistenceStore
from src.models import ATMResult, Candle, OptionChainSnapshot
from src.paper_trading import PaperPosition
from src.persistence import PersistenceStore
from src.timeutil import get_zone
from tests.conftest import external_test_database_url

IST = get_zone("Asia/Kolkata")


@pytest.fixture
def dest(monkeypatch, tmp_path):
    external = external_test_database_url()
    monkeypatch.setenv("DATABASE_URL", external or f"sqlite:///{(tmp_path / 'dest.db').as_posix()}")
    db_session.reset_engine()
    if external:
        downgrade_to_base()
    upgrade_to_head()
    yield
    db_session.reset_engine()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "legacy.db"
    store = PersistenceStore(path)
    day1, day2 = datetime(2026, 9, 10, 9, 15, tzinfo=IST), datetime(2026, 9, 11, 9, 15, tzinfo=IST)
    for day in (day1, day2):
        for i in range(3):
            start = day + timedelta(minutes=i)
            store.save_candle(Candle("NIFTY", 1, start, start + timedelta(minutes=1), 100 + i, 102 + i, 99, 101 + i, 4, True))
            store.save_candle(Candle("24800.0_ce", 1, start, start + timedelta(minutes=1), 50, 52, 49, 51, 2, True))
        store.save_option_chain_snapshot(
            OptionChainSnapshot(underlying="NIFTY", underlying_ltp=24800.0, expiry="22-Oct-2026", timestamp=day + timedelta(seconds=1), strikes=[]),
            ATMResult(strike=24800.0, call_ltp=100.0, put_ltp=95.0, straddle_premium=195.0, underlying_ltp=24800.0, distance_from_spot=0.0),
        )
    for pid, entry, pnl in (("a1", day1 + timedelta(hours=1), 250.0), ("a2", day2 + timedelta(hours=1), -100.0)):
        store.save_paper_trade(PaperPosition(
            id=pid, option_type="CE", strike=24800.0, quantity=50, entry_price=100.0, entry_time=entry, take_profit_percent=10.0,
            stop_loss_percent=5.0, side="BUY", exit_price=105.0, exit_time=entry + timedelta(minutes=3), exit_reason="TAKE_PROFIT",
            pnl=pnl, pnl_percent=5.0, status="EXITED"))
    store.close()
    return path


def counts():
    with db_session.session_scope() as s:
        return (s.scalar(select(func.count()).select_from(MarketCandle)), s.scalar(select(func.count()).select_from(MarketChainSnapshot)))


def test_history_import_copies_everything_and_matches_the_original(dest, source):
    result = import_history(source)
    assert result["candles"] == 12 and result["chain_snapshots"] == 2 and result["legacy_paper_trades"] == 2
    old, new = PersistenceStore(source), SqlPersistenceStore()
    try:
        assert new.list_trading_dates() == old.list_trading_dates()
        for day in ("2026-09-10", "2026-09-11"):
            assert new.get_candles_for_date("NIFTY", 1, day) == old.get_candles_for_date("NIFTY", 1, day)
            assert new.get_option_chain_snapshots_for_date("NIFTY", day) == old.get_option_chain_snapshots_for_date("NIFTY", day)
            assert new.list_strikes_for_date(day) == old.list_strikes_for_date(day) == [24800.0]
            assert new.get_paper_trades_for_date(day) == old.get_paper_trades_for_date(day)
    finally:
        old.close()


def test_history_import_is_safe_to_run_twice(dest, source):
    import_history(source)
    again = import_history(source)
    assert again == {"candles": 0, "chain_snapshots": 0, "legacy_paper_trades": 0}
    assert counts() == (12, 2)


def test_history_import_adds_only_new_days_on_a_later_run(dest, source):
    import_history(source)
    store = PersistenceStore(source)
    day3 = datetime(2026, 9, 14, 9, 15, tzinfo=IST)
    store.save_candle(Candle("NIFTY", 1, day3, day3 + timedelta(minutes=1), 1, 2, 0, 1, 1, True))
    store.save_option_chain_snapshot(OptionChainSnapshot(underlying="NIFTY", underlying_ltp=1.0, expiry=None, timestamp=day3, strikes=[]), None)
    store.close()
    added = import_history(source)
    assert added["candles"] == 1 and added["chain_snapshots"] == 1
    assert counts() == (13, 3)


def test_the_source_file_is_never_modified(dest, source):
    before = source.read_bytes()
    import_history(source)
    assert source.read_bytes() == before


def register(email="trader@example.com"):
    with db_session.session_scope() as s:
        s.add(User(email=email, password_hash="x"))


def test_paper_trades_land_in_the_users_book_with_their_numbers(dest, source):
    register()
    result = import_paper_trades(source, "Trader@Example.com")  # email matching is case-insensitive
    assert result == {"imported": 2, "already_present": 0, "skipped": 0}
    with db_session.session_scope() as s:
        rows = s.scalars(select(PaperPositionRow).order_by(PaperPositionRow.entry_time)).all()
        assert [(r.id, r.status, r.pnl, r.exit_reason, r.trading_date) for r in rows] == [
            ("a1", "EXITED", 250.0, "TAKE_PROFIT", "2026-09-10"),
            ("a2", "EXITED", -100.0, "TAKE_PROFIT", "2026-09-11"),
        ]
        assert all(r.exit_time is not None and r.exit_time.tzinfo is not None for r in rows)


def test_paper_trade_import_is_idempotent_and_does_not_touch_other_users(dest, source):
    register()
    register("other@example.com")
    import_paper_trades(source, "trader@example.com")
    assert import_paper_trades(source, "trader@example.com") == {"imported": 0, "already_present": 2, "skipped": 0}
    with db_session.session_scope() as s:
        other = s.scalar(select(User).where(User.email == "other@example.com"))
        assert s.scalar(select(func.count()).select_from(PaperPositionRow).where(PaperPositionRow.user_id == other.id)) == 0


def test_importing_for_an_unknown_user_fails_clearly(dest, source):
    with pytest.raises(LookupError, match="Register first"):
        import_paper_trades(source, "nobody@example.com")


def test_cli_reports_errors_without_a_traceback(dest, tmp_path, capsys):
    assert main(["history", "--sqlite", str(tmp_path / "missing.db")]) == 1
    assert "No SQLite file" in capsys.readouterr().err
    assert main(["paper-trades", "--sqlite", str(tmp_path / "missing.db"), "--email", "a@b.co"]) == 1
