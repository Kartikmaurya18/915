"""The core multi-user guarantee: one user's paper trading never touches, reveals
or depends on another's - and survives restarts."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.api import server
from src.db.models import AuditLog, IdempotencyKey, PaperPositionRow
from src.db.session import session_scope
from src.paper import service as paper_service
from src.paper.exit_worker import PaperExitWorker


def _open(client, headers, contract, option_type="CE", quantity=50, **extra):
    strike, expiry, underlying = contract
    return client.post(
        "/api/paper/open",
        json={"option_type": option_type, "strike": strike, "quantity": quantity, "side": "BUY",
              "expiry": expiry, "underlying": underlying},
        headers={**headers, **extra},
    )


def _state(client, headers):
    response = client.get("/api/paper/state", headers=headers)
    assert response.status_code == 200
    return response.json()


# -- isolation ---------------------------------------------------------------------


def test_users_have_independent_books(client, new_user, contract):
    _, alice = new_user()
    _, bob = new_user()
    opened = _open(client, alice, contract)
    assert opened.status_code == 200, opened.text

    assert len(_state(client, alice)["open_positions"]) == 1
    bob_state = _state(client, bob)
    assert bob_state["open_positions"] == [] and bob_state["history"] == []
    assert bob_state["pnl_summary"]["open_position_count"] == 0


def test_a_user_cannot_close_someone_elses_position(client, new_user, contract):
    _, alice = new_user()
    _, mallory = new_user()
    position_id = _open(client, alice, contract).json()["id"]

    attempt = client.post(f"/api/paper/close/{position_id}", headers=mallory)
    assert attempt.status_code == 404  # indistinguishable from "no such position"
    assert client.post("/api/paper/close/does-not-exist", headers=mallory).json() == attempt.json()

    still_open = _state(client, alice)["open_positions"]
    assert [p["id"] for p in still_open] == [position_id]
    assert _state(client, alice)["history"] == []


def test_the_owner_can_close_their_own_position(client, new_user, contract):
    _, alice = new_user()
    position_id = _open(client, alice, contract).json()["id"]
    closed = client.post(f"/api/paper/close/{position_id}", headers=alice)
    assert closed.status_code == 200 and closed.json()["status"] == "EXITED"
    assert client.post(f"/api/paper/close/{position_id}", headers=alice).status_code == 404  # already closed
    state = _state(client, alice)
    assert state["open_positions"] == [] and len(state["history"]) == 1


def test_take_profit_settings_are_per_user(client, new_user, contract):
    _, alice = new_user()
    _, bob = new_user()
    changed = client.post("/api/paper/config", json={"take_profit_percent": 42, "stop_loss_percent": 7}, headers=bob)
    assert changed.status_code == 200 and changed.json()["take_profit_percent"] == 42

    assert _state(client, bob)["config"]["take_profit_percent"] == 42
    assert _state(client, alice)["config"]["take_profit_percent"] == 10.0  # untouched default
    opened = _open(client, alice, contract).json()
    assert opened["take_profit_percent"] == 10.0


def test_invalid_config_is_rejected_and_does_not_change_state(client, new_user):
    _, alice = new_user()
    assert client.post("/api/paper/config", json={"take_profit_percent": -5}, headers=alice).status_code == 400
    assert _state(client, alice)["config"]["take_profit_percent"] == 10.0


def test_auto_exit_fires_only_for_the_user_whose_threshold_was_hit(client, new_user, contract, chain_with_call_move):
    strike = contract[0]
    _, alice = new_user()  # default take-profit: +10%
    _, bob = new_user()
    client.post("/api/paper/config", json={"take_profit_percent": 500}, headers=bob)
    alice_id = _open(client, alice, contract).json()["id"]
    bob_id = _open(client, bob, contract).json()["id"]

    PaperExitWorker().on_chain(chain_with_call_move(strike, 1.25), datetime.now(timezone.utc))  # CE +25%

    alice_state = _state(client, alice)
    assert alice_state["open_positions"] == []
    assert alice_state["history"][0]["id"] == alice_id
    assert alice_state["history"][0]["exit_reason"] == "TAKE_PROFIT"
    assert alice_state["history"][0]["pnl"] > 0

    bob_state = _state(client, bob)
    assert [p["id"] for p in bob_state["open_positions"]] == [bob_id]  # +25% < his +500% target
    assert bob_state["history"] == []

    with session_scope() as session:  # the exit was written through to the database
        row = session.get(PaperPositionRow, (_user_id(session, alice_id), alice_id))
        assert row.status == "EXITED" and row.exit_reason == "TAKE_PROFIT"


def _user_id(session, position_id):
    return session.query(PaperPositionRow.user_id).filter_by(id=position_id).scalar()


def test_stop_loss_exit_is_per_user_and_persisted(client, new_user, contract, chain_with_call_move):
    strike = contract[0]
    _, alice = new_user()
    position_id = _open(client, alice, contract).json()["id"]
    PaperExitWorker().on_chain(chain_with_call_move(strike, 0.8), datetime.now(timezone.utc))  # CE -20%
    history = _state(client, alice)["history"]
    assert history[0]["exit_reason"] == "STOP_LOSS" and history[0]["pnl"] < 0
    assert history[0]["id"] == position_id


# -- durability --------------------------------------------------------------------


def test_positions_and_history_survive_a_restart(client, new_user, contract, chain_with_call_move):
    _, alice = new_user()
    kept_open = _open(client, alice, contract).json()["id"]
    closed_id = _open(client, alice, contract, option_type="PE").json()["id"]
    client.post(f"/api/paper/close/{closed_id}", headers=alice)
    client.post("/api/paper/config", json={"take_profit_percent": 33}, headers=alice)

    server.reset_paper_service()  # simulate a process restart: all in-memory books are gone
    state = _state(client, alice)
    assert [p["id"] for p in state["open_positions"]] == [kept_open]
    assert [p["id"] for p in state["history"]] == [closed_id]
    assert state["config"]["take_profit_percent"] == 33


def test_exits_are_driven_by_the_database_so_a_new_worker_needs_no_warm_up(client, new_user, contract, chain_with_call_move):
    """There is no in-memory book to rebuild after a restart or failover: a brand new exit
    worker (a new leader) evaluates whatever the database says is open."""
    strike = contract[0]
    _, alice = new_user()
    _open(client, alice, contract)
    server.reset_paper_service()

    PaperExitWorker().on_chain(chain_with_call_move(strike, 1.3), datetime.now(timezone.utc))
    assert _state(client, alice)["history"][0]["exit_reason"] == "TAKE_PROFIT"


def test_a_failed_database_write_does_not_leave_a_phantom_position(client, new_user, contract, monkeypatch):
    _, alice = new_user()

    def boom(*_a, **_k):
        raise RuntimeError("database down")

    with monkeypatch.context() as scoped:  # undo only this patch, not the fixture's
        scoped.setattr(paper_service, "position_to_row", boom)
        with pytest.raises(RuntimeError):
            _open(client, alice, contract)
    assert _state(client, alice)["open_positions"] == []


# -- retries, limits ---------------------------------------------------------------


def test_idempotency_key_makes_open_safe_to_retry(client, new_user, contract):
    _, alice = new_user()
    first = _open(client, alice, contract, **{"Idempotency-Key": "order-1"})
    retry = _open(client, alice, contract, **{"Idempotency-Key": "order-1"})
    assert first.status_code == retry.status_code == 200
    assert first.json()["id"] == retry.json()["id"]
    assert len(_state(client, alice)["open_positions"]) == 1  # not two

    other = _open(client, alice, contract, **{"Idempotency-Key": "order-2"})
    assert other.json()["id"] != first.json()["id"]
    assert len(_state(client, alice)["open_positions"]) == 2


def test_idempotency_keys_are_scoped_to_the_user(client, new_user, contract):
    _, alice = new_user()
    _, bob = new_user()
    a = _open(client, alice, contract, **{"Idempotency-Key": "same-key"}).json()
    b = _open(client, bob, contract, **{"Idempotency-Key": "same-key"}).json()
    assert a["id"] != b["id"]  # bob never receives alice's stored response
    with session_scope() as session:
        assert session.query(IdempotencyKey).count() == 2


def test_open_position_limit_is_enforced_per_user(client, new_user, contract, monkeypatch):
    monkeypatch.setattr(paper_service, "MAX_OPEN_POSITIONS", 2)
    _, alice = new_user()
    _, bob = new_user()
    assert _open(client, alice, contract).status_code == 200
    assert _open(client, alice, contract).status_code == 200
    blocked = _open(client, alice, contract)
    assert blocked.status_code == 400 and "limit" in blocked.json()["detail"].lower()
    assert _open(client, bob, contract).status_code == 200  # bob's budget is his own


@pytest.mark.parametrize("payload", [
    {"option_type": "CE", "quantity": 0},
    {"option_type": "CE", "quantity": -3},
    {"option_type": "CE", "quantity": 10**9},
    {"option_type": "CE" * 40},
    {"option_type": "XX", "quantity": 1},
])
def test_open_rejects_invalid_input(client, new_user, payload):
    _, alice = new_user()
    assert client.post("/api/paper/open", json=payload, headers=alice).status_code in (400, 422)


def test_open_rejects_a_contract_that_is_not_in_the_chain(client, new_user, contract):
    _, alice = new_user()
    strike, expiry, underlying = contract
    bad_strike = client.post("/api/paper/open", json={"option_type": "CE", "strike": 1.0}, headers=alice)
    bad_expiry = client.post("/api/paper/open", json={"option_type": "CE", "strike": strike, "expiry": "01-Jan-2000"}, headers=alice)
    assert bad_strike.status_code == 400 and bad_expiry.status_code == 400
    assert _state(client, alice)["open_positions"] == []


# -- audit & history ---------------------------------------------------------------


def test_trading_actions_are_audited(client, new_user, contract):
    _, alice = new_user()
    position_id = _open(client, alice, contract).json()["id"]
    client.post(f"/api/paper/close/{position_id}", headers=alice)
    client.post("/api/paper/config", json={"take_profit_percent": 12}, headers=alice)
    with session_scope() as session:
        events = {e.event for e in session.query(AuditLog)}
    assert {"register", "paper_open", "paper_close", "paper_config_changed"} <= events


def test_historical_session_shows_only_the_callers_trades(client, new_user, contract):
    _, alice = new_user()
    _, bob = new_user()
    opened = _open(client, alice, contract).json()
    client.post(f"/api/paper/close/{opened['id']}", headers=alice)
    trade_date = opened["entry_time"][:10]  # IST calendar date of the entry

    alice_history = client.get(f"/api/history/session?date={trade_date}", headers=alice).json()["paper_trading"]
    bob_history = client.get(f"/api/history/session?date={trade_date}", headers=bob).json()["paper_trading"]
    assert [t["id"] for t in alice_history["history"]] == [opened["id"]]
    assert bob_history["history"] == []
    assert alice_history["stats"]["total_trades"] == 1 and bob_history["stats"]["total_trades"] == 0


def test_the_global_snapshot_never_carries_any_users_book(client, new_user, contract):
    _, alice = new_user()
    _open(client, alice, contract)
    _, bob = new_user()
    assert "paper_trading" not in client.get("/api/snapshot", headers=bob).json()


def test_deleting_a_user_removes_their_trading_data(client, new_user, contract):
    email, alice = new_user()
    _open(client, alice, contract)
    from src.db.models import User

    with session_scope() as session:
        session.delete(session.query(User).filter_by(email=email).one())
    with session_scope() as session:
        assert session.query(PaperPositionRow).count() == 0  # ON DELETE CASCADE
