"""Shared fixtures for the multi-user (AUTH_MODE=required) tests."""

from __future__ import annotations

import copy
import itertools
import os

import pytest

# Tests never need a GUI. Matplotlib's default Tk backend intermittently fails to
# initialise on Windows when other threads are alive (seen once in the RRG tests).
os.environ.setdefault("MPLBACKEND", "Agg")

JWT_TEST_SECRET = "test-secret-" + "x" * 40
_counter = itertools.count(1)
USER_TABLES = (
    "audit_log", "idempotency_keys", "paper_positions", "paper_settings", "refresh_tokens", "users",
)


def external_test_database_url() -> str | None:
    """Optional PostgreSQL to run the DB tests against: set TEST_DATABASE_URL.

    Refuses any database whose name does not contain "test", so a mistyped URL can
    never point automated tests (which wipe tables) at real data.
    """
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        return None
    from sqlalchemy.engine import make_url

    name = make_url(url).database or ""
    if "test" not in name.lower():
        pytest.exit(f"Refusing to run tests against database {name!r}: its name must contain 'test'.", returncode=2)
    return url


@pytest.fixture
def auth_env(monkeypatch, tmp_path):
    """AUTH_MODE=required against a fresh, fully migrated SQLite database.

    The schema is built by running the real Alembic migrations, so every test in
    this group also exercises them.
    """
    from src.api import auth_routes, server
    from src.db import session as db_session
    from src.db.migrate import upgrade_to_head

    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("JWT_SECRET", JWT_TEST_SECRET)
    external = external_test_database_url()
    monkeypatch.setenv("DATABASE_URL", external or f"sqlite:///{(tmp_path / 'users.db').as_posix()}")
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("REGISTRATION_OPEN", raising=False)

    server.reset_paper_service()
    db_session.reset_engine()
    upgrade_to_head()
    if external:  # shared database: start every test from empty tables
        from sqlalchemy import text

        with db_session.get_engine().begin() as connection:
            connection.execute(text("TRUNCATE " + ", ".join(USER_TABLES) + " RESTART IDENTITY CASCADE"))
    for limiter in (auth_routes.login_by_ip, auth_routes.login_by_account, auth_routes.register_by_ip):
        limiter.clear()
    yield
    server.reset_paper_service()
    db_session.reset_engine()


@pytest.fixture
def client(auth_env, monkeypatch):
    """A TestClient running the real app lifespan.

    The engine's background tick thread is disabled: these tests drive ticks
    explicitly via `engine.tick_once()`, which keeps them deterministic and avoids
    each teardown waiting on a worker that is asleep between polls.
    """
    from fastapi.testclient import TestClient

    from src.api.server import app, engine

    monkeypatch.setattr(engine, "start", lambda: None)
    with TestClient(app) as test_client:
        engine.tick_once()  # make sure an option chain exists before any trade
        yield test_client


@pytest.fixture
def new_user(client):
    """Factory: register a user, return (email, bearer-header dict)."""

    def _make(password: str = "correct horse battery") -> tuple[str, dict]:
        email = f"user{next(_counter)}@example.com"
        response = client.post("/api/auth/register", json={"email": email, "password": password})
        assert response.status_code == 201, response.text
        return email, {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _make


@pytest.fixture
def contract():
    """(strike, expiry, underlying) of a real strike in the engine's current chain."""
    from src.api.server import engine

    chain = engine._latest_chain
    assert chain is not None, "engine has no option chain; tick_once() produced nothing"
    leg = chain.strikes[len(chain.strikes) // 2]
    return leg.strike, chain.expiry, chain.underlying


@pytest.fixture
def chain_with_call_move():
    """Factory: a deep copy of the current chain with one strike's CE premium scaled."""
    from src.api.server import engine

    def _make(strike: float, factor: float):
        chain = copy.deepcopy(engine._latest_chain)
        leg = chain.get_leg(strike)
        leg.call_ltp = round(leg.call_ltp * factor, 2)
        return chain

    return _make
