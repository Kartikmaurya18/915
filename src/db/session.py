"""Database engine and session management (SQLAlchemy 2.x, synchronous).

`DATABASE_URL` selects the backend. Production is managed PostgreSQL; the
default is a local SQLite file so development and tests need no services.
Provider-specific details (Neon, Supabase, RDS, ...) live entirely in the URL
and the pool/timeout environment variables below - no code depends on them.
"""

from __future__ import annotations

import os
import random
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from prometheus_client import Counter
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

T = TypeVar("T")

transaction_retries = Counter("db_transaction_retries_total", "Transactions retried after a deadlock or lock timeout", ["reason"])

DEFAULT_SQLITE_URL = "sqlite:///data/app.db"

_lock = threading.Lock()
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def normalize_database_url(url: str) -> str:
    """Accept the common `postgres://` / `postgresql://` spellings and select
    the psycopg v3 driver, which is the one we ship."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def database_url() -> str:
    return normalize_database_url(os.getenv("DATABASE_URL") or DEFAULT_SQLITE_URL)


def _build_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        if ":memory:" not in url:
            path = url.split("///", 1)[-1]
            if path:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine

    # PostgreSQL: bounded pool, liveness checks (serverless Postgres drops idle
    # connections), and a server-side statement timeout so one slow query can
    # never pin a pooled connection indefinitely.
    statement_timeout_ms = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "10000"))
    return create_engine(
        url,
        pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
        pool_timeout=float(os.getenv("DB_POOL_TIMEOUT_SECONDS", "10")),
        pool_recycle=int(os.getenv("DB_POOL_RECYCLE_SECONDS", "1800")),
        pool_pre_ping=True,
        connect_args={"options": f"-c statement_timeout={statement_timeout_ms}"},
    )


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = _build_engine(database_url())
                _session_factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """One transaction: commit on success, roll back on any exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# PostgreSQL: 40P01 deadlock_detected, 40001 serialization_failure. SQLite: a busy database.
_RETRYABLE_SQLSTATES = {"40P01": "deadlock", "40001": "serialization"}


def _retry_reason(exc: OperationalError) -> str | None:
    state = getattr(exc.orig, "sqlstate", None)
    if state in _RETRYABLE_SQLSTATES:
        return _RETRYABLE_SQLSTATES[state]
    if "database is locked" in str(exc.orig).lower():
        return "locked"
    return None


def run_in_transaction(work: Callable[[Session], T], retries: int = 3) -> T:
    """Run `work(session)` in one transaction, retrying the WHOLE thing if the database picked
    it as a deadlock victim (or reports a serialization failure). Safe because a victim has
    already been rolled back; `work` must therefore hold no side effects outside the session."""
    for attempt in range(retries + 1):
        try:
            with session_scope() as session:
                return work(session)
        except OperationalError as exc:
            reason = _retry_reason(exc)
            if reason is None or attempt >= retries:
                raise
            transaction_retries.labels(reason).inc()
            time.sleep(random.uniform(0.005, 0.03) * (attempt + 1))  # jitter, so the victims do not collide again
    raise AssertionError("unreachable")


def reset_engine() -> None:
    """Dispose the cached engine so the next call re-reads DATABASE_URL (tests)."""
    global _engine, _session_factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None


def create_all_for_tests() -> None:
    """Create tables straight from the models. Production uses Alembic only."""
    from src.db.models import Base

    Base.metadata.create_all(get_engine())
