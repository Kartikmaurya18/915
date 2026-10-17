"""Retention and housekeeping jobs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from src.db import session as db_session
from src.db.maintenance import main, purge_expired, purge_history, stats
from src.db.migrate import downgrade_to_base, upgrade_to_head
from src.db.models import IdempotencyKey, MarketCandle, RefreshToken, User
from src.timeutil import today_ist
from tests.conftest import external_test_database_url


@pytest.fixture
def db(monkeypatch, tmp_path):
    external = external_test_database_url()
    monkeypatch.setenv("DATABASE_URL", external or f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    db_session.reset_engine()
    if external:
        downgrade_to_base()
    upgrade_to_head()
    yield
    db_session.reset_engine()


def add_candle(day: str, minute: int = 0):
    with db_session.session_scope() as s:
        s.add(MarketCandle(symbol="NIFTY", interval_minutes=1, open_time=f"{day}T09:{minute:02d}:00+05:30", close_time=f"{day}T09:{minute + 1:02d}:00+05:30",
                           open=1, high=2, low=0, close=1, tick_count=1, trading_date=day))


def candle_days():
    with db_session.session_scope() as s:
        return sorted(s.scalars(select(MarketCandle.trading_date).distinct()))


def test_purge_history_keeps_the_window_and_never_touches_today(db):
    today = today_ist()
    days = [(today - timedelta(days=n)).isoformat() for n in (200, 100, 91, 90, 10, 0)]
    for d in days:
        add_candle(d)
    removed = purge_history(keep_days=90)
    assert removed == 3  # 200, 100 and 91 days ago
    assert candle_days() == sorted(days[3:])  # 90 days ago is still inside the window; today is untouched


def test_purge_history_refuses_a_zero_window(db):
    with pytest.raises(ValueError):
        purge_history(0)
    assert main(["purge-history", "--keep-days", "0"]) == 2


def test_purge_history_is_repeatable(db):
    add_candle((today_ist() - timedelta(days=400)).isoformat())
    assert purge_history(30) == 1
    assert purge_history(30) == 0


def test_purge_expired_removes_only_what_can_no_longer_matter(db):
    now = datetime.now(timezone.utc)
    with db_session.session_scope() as s:
        user = User(email="a@b.co", password_hash="x")
        s.add(user)
        s.flush()
        for name, age in (("old", 5), ("fresh", 0)):
            s.add(IdempotencyKey(user_id=user.id, key=name, response={}, created_at=now - timedelta(days=age)))
        for i, expires in enumerate((now - timedelta(days=30), now - timedelta(days=1), now + timedelta(days=10))):
            s.add(RefreshToken(user_id=user.id, family_id="f", token_hash=f"h{i}", expires_at=expires, created_at=now - timedelta(days=40)))
    result = purge_expired()
    assert result == {"idempotency_keys": 1, "refresh_tokens": 1}  # only the 5-day-old key and the token expired 30 days ago
    with db_session.session_scope() as s:
        assert s.scalar(select(func.count()).select_from(IdempotencyKey)) == 1
        assert s.scalar(select(func.count()).select_from(RefreshToken)) == 2  # the recently expired one is kept for replay detection


def test_stats_reports_counts_and_the_history_range(db, capsys):
    add_candle("2026-09-01")
    add_candle("2026-09-03")
    info = stats()
    assert info["market_candles"] == 2 and (info["history_from"], info["history_to"]) == ("2026-09-01", "2026-09-03")
    assert main(["stats"]) == 0
    assert "market_candles" in capsys.readouterr().out
