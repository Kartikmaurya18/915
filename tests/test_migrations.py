"""Alembic migrations and schema behaviour (SQLite here; see docs for PostgreSQL)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from src.db import session as db_session
from src.db.migrate import downgrade_to_base, schema_status, upgrade_to_head
from src.db.models import Base, PaperPositionRow, User, UTCDateTime
from src.db.session import get_engine, session_scope
from tests.conftest import external_test_database_url

EXPECTED_TABLES = {
    "users", "refresh_tokens", "paper_settings", "paper_positions", "idempotency_keys", "audit_log",
    "market_candles", "market_chain_snapshots", "legacy_paper_trades",
}


@pytest.fixture
def empty_db(monkeypatch, tmp_path):
    external = external_test_database_url()
    monkeypatch.setenv("DATABASE_URL", external or f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    db_session.reset_engine()
    if external:  # shared database: return to an unmigrated state first
        downgrade_to_base()
    yield
    db_session.reset_engine()


def test_upgrade_creates_every_table_and_marks_the_head(empty_db):
    assert schema_status()[0] is False
    upgrade_to_head()
    assert EXPECTED_TABLES <= set(inspect(get_engine()).get_table_names())
    assert schema_status()[0] is True


def test_downgrade_removes_everything(empty_db):
    upgrade_to_head()
    downgrade_to_base()
    assert not (EXPECTED_TABLES & set(inspect(get_engine()).get_table_names()))


def test_upgrade_is_idempotent(empty_db):
    upgrade_to_head()
    upgrade_to_head()
    assert schema_status()[0] is True


def test_models_and_migrations_have_not_drifted(empty_db):
    """Fails if someone edits a model without adding a migration."""
    upgrade_to_head()
    with get_engine().connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata)
    assert diff == [], f"models differ from migrations: {diff}"


def test_naive_datetimes_are_refused_and_timezones_round_trip_as_utc(empty_db):
    upgrade_to_head()
    with pytest.raises(Exception, match="naive"):
        UTCDateTime().process_bind_param(datetime(2026, 1, 1), None)
    with session_scope() as session:
        user = User(email="t@example.com", password_hash="x")
        session.add(user)
        session.flush()
        user.last_login_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        user_id = user.id
    with session_scope() as session:
        stored = session.get(User, user_id).last_login_at
        assert stored.tzinfo is not None and stored.utcoffset().total_seconds() == 0


def test_foreign_keys_are_enforced(empty_db):
    upgrade_to_head()
    with pytest.raises(Exception):
        with session_scope() as session:
            session.add(
                PaperPositionRow(
                    user_id="no-such-user", id="p1", option_type="CE", side="BUY", strike=1.0, quantity=1,
                    entry_price=1.0, current_price=1.0, entry_time=datetime.now(timezone.utc),
                    take_profit_percent=10, stop_loss_percent=5, trading_date="2026-01-01",
                )
            )


def test_email_is_unique(empty_db):
    upgrade_to_head()
    with session_scope() as session:
        session.add(User(email="dup@example.com", password_hash="x"))
    with pytest.raises(Exception):
        with session_scope() as session:
            session.add(User(email="dup@example.com", password_hash="y"))


@pytest.mark.parametrize("raw,expected", [
    ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ("postgresql://u:p@h/db?sslmode=require", "postgresql+psycopg://u:p@h/db?sslmode=require"),
    ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ("sqlite:///x.db", "sqlite:///x.db"),
])
def test_database_urls_are_normalised(raw, expected):
    assert db_session.normalize_database_url(raw) == expected
