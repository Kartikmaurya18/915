"""Races: the guarantees that only hold if the database, not luck, enforces them."""

from __future__ import annotations

import threading
from collections import Counter

from sqlalchemy import func, select

from src.api import server
from src.auth import service
from src.auth.security import AuthError
from src.db.models import PaperPositionRow, RefreshToken, User
from src.db.session import run_in_transaction, session_scope

THREADS = 8
# A race is only exposed some of the time, so each test repeats it with fresh
# tokens/keys: a correct implementation passes every round, a racy one is caught
# with near certainty (verified by mutation testing against a non-atomic claim).
ROUNDS = 25


def _run_together(fn, count=THREADS):
    """Run `fn(i)` on `count` threads released at the same instant; return results/exceptions."""
    barrier = threading.Barrier(count)
    results: list = [None] * count

    def worker(i: int) -> None:
        barrier.wait()
        try:
            results[i] = fn(i)
        except Exception as exc:  # collected, asserted on by the caller
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a worker hung"
    return results


def _register(email="race@example.com"):
    with session_scope() as session:
        user = service.register(session, email, "correct horse battery")
        return user.id, service.start_session(session, user).refresh_token


def test_exactly_one_concurrent_refresh_wins(client):
    user_id, _ = _register()
    for round_no in range(ROUNDS):
        with session_scope() as session:  # a fresh, unspent token for this round
            raw = service.start_session(session, session.get(User, user_id)).refresh_token

        def refresh(_i, raw=raw):
            return run_in_transaction(lambda s: service.refresh(s, raw)).access_token  # as the route does

        outcomes = _run_together(refresh)
        winners = [o for o in outcomes if isinstance(o, str)]
        losers = [o for o in outcomes if isinstance(o, AuthError)]
        assert len(winners) == 1, f"round {round_no}: {len(winners)} winners: {outcomes}"
        assert len(losers) == THREADS - 1, f"round {round_no}: {outcomes}"
        assert {e.code for e in losers} == {"refresh_token_reused"}


def test_losing_a_refresh_race_revokes_the_session_family(client):
    """Strict by design: a replay revokes the whole family - including the winner's
    fresh token - so a stolen token cannot be raced for a live session. The cost is
    that two browser tabs refreshing simultaneously can sign the user out; the SPA
    must serialise refreshes (single-flight)."""
    user_id, raw = _register("strict@example.com")

    def refresh(_i):
        return run_in_transaction(lambda s: service.refresh(s, raw))

    _run_together(refresh)
    with session_scope() as session:
        live = session.scalar(
            select(func.count()).select_from(RefreshToken).where(
                RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
            )
        )
    assert live == 0


def test_concurrent_opens_with_one_idempotency_key_create_one_position(client, contract):
    strike, expiry, underlying = contract
    user_id, _ = _register("idem@example.com")
    svc = server.get_paper_service()

    for round_no in range(ROUNDS // 2):
        key = f"race-key-{round_no}"

        def open_it(_i, key=key):
            return svc.open_position(user_id, "CE", strike, 50, "BUY", expiry, underlying, idempotency_key=key)

        outcomes = _run_together(open_it)
        errors = [o for o in outcomes if isinstance(o, Exception)]
        assert not errors, f"round {round_no}: {errors}"
        assert len({o["id"] for o in outcomes}) == 1, f"round {round_no}: callers got different positions"

    expected = ROUNDS // 2  # exactly one position per key, never one per caller
    with session_scope() as session:
        stored = session.scalar(select(func.count()).select_from(PaperPositionRow).where(PaperPositionRow.user_id == user_id))
    assert stored == expected
    assert len(svc.state(user_id)["open_positions"]) == expected  # and no ghosts left in memory


def test_many_users_trading_at_once_never_cross(client, contract):
    strike, expiry, underlying = contract
    svc = server.get_paper_service()
    users = [_register(f"multi{i}@example.com")[0] for i in range(6)]
    per_user = 4

    def trade(i):
        uid = users[i % len(users)]
        return uid, svc.open_position(uid, "CE", strike, 10 + i, "BUY", expiry, underlying)["id"]

    outcomes = _run_together(trade, count=len(users) * per_user)
    assert not [o for o in outcomes if isinstance(o, Exception)], outcomes
    expected = Counter(uid for uid, _ in outcomes)
    for uid in users:
        assert len(svc.state(uid)["open_positions"]) == expected[uid] == per_user
        with session_scope() as session:
            rows = session.scalars(select(PaperPositionRow.id).where(PaperPositionRow.user_id == uid)).all()
        assert sorted(rows) == sorted(pid for owner, pid in outcomes if owner == uid)
