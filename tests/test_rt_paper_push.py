"""Paper-book change notices: reach only the owner's sockets, coalesce, and fire on every kind of change."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone

import pytest

from src.api import server
from src.realtime import hub as hub_module
from src.realtime.hub import LOCAL_USER_KEY, Hub

from tests.test_rt_ws import auth_frame, publish_tick, recv_until  # noqa: F401  (fresh_hub fixture below)


# -- the hub --------------------------------------------------------------------------------


def make_client(hub, user_id):
    frames: list[dict] = []

    async def send(frame: str):
        frames.append(json.loads(frame))

    async def close(code, reason):
        pass

    return hub.add_client(send, close, user_id), frames


def notices(frames):
    return [f for f in frames if f["type"] == "paper_changed"]


def run(coro):
    return asyncio.run(coro)


def test_a_notice_reaches_every_socket_of_that_user_and_nobody_else():
    async def scenario():
        hub = Hub()
        a1, f_a1 = make_client(hub, "alice")
        a2, f_a2 = make_client(hub, "alice")
        b, f_b = make_client(hub, "bob")
        assert hub.notify_user("alice") == 2
        await asyncio.sleep(0.05)
        assert len(notices(f_a1)) == len(notices(f_a2)) == 1
        assert notices(f_b) == []
        for c in (a1, a2, b):
            await c.shutdown()

    run(scenario())


def test_many_changes_before_the_next_send_become_one_notice():
    async def scenario():
        hub = Hub()
        client, frames = make_client(hub, "alice")
        for _ in range(500):
            hub.notify_user("alice")  # e.g. 500 positions auto-closing on one tick
        await asyncio.sleep(0.05)
        assert len(notices(frames)) == 1
        assert not client.closed  # the control queue was never touched, so it cannot overflow
        hub.notify_user("alice")  # a later change gets its own notice
        await asyncio.sleep(0.05)
        assert len(notices(frames)) == 2
        await client.shutdown()

    run(scenario())


def test_a_notice_does_not_disturb_market_data_delivery():
    async def scenario():
        hub = Hub()
        client, frames = make_client(hub, "alice")
        client.subscribe(["options:NIFTY"])
        hub.apply_update(hub_module.TopicUpdate("options:NIFTY", 1, 1, 1, 1, {"strikes": []}))
        hub.notify_user("alice")
        await asyncio.sleep(0.05)
        assert {f["type"] for f in frames} == {"snapshot", "paper_changed"}
        await client.shutdown()

    run(scenario())


def test_disconnected_sockets_are_forgotten():
    async def scenario():
        hub = Hub()
        client, _ = make_client(hub, "alice")
        assert hub.notify_user("alice") == 1
        await client.shutdown()
        assert hub.notify_user("alice") == 0
        assert "alice" not in hub._by_user  # no leak of empty buckets

    run(scenario())


def test_a_server_without_auth_notifies_its_anonymous_sockets():
    async def scenario():
        hub = Hub()
        client, frames = make_client(hub, None)
        assert hub.notify_user(LOCAL_USER_KEY) == 1
        await asyncio.sleep(0.05)
        assert len(notices(frames)) == 1
        await client.shutdown()

    run(scenario())


def test_notifying_an_unknown_user_is_harmless():
    assert Hub().notify_user("nobody") == 0


# -- the exit worker ----------------------------------------------------------------------------


def test_exit_worker_reports_affected_users_after_commit(client, new_user, contract, chain_with_call_move):
    from src.db.models import PaperPositionRow
    from src.db.session import session_scope
    from src.paper.exit_worker import PaperExitWorker

    seen: list[tuple[set[str], str]] = []

    def on_exits(users):
        with session_scope() as session:  # committed already: a refetching client would see the exit
            statuses = {r.status for r in session.query(PaperPositionRow).all()}
        seen.append((set(users), ",".join(sorted(statuses))))

    _, alice = new_user()
    _, bob = new_user()
    client.post("/api/paper/config", json={"take_profit_percent": 500}, headers=bob)
    for headers in (alice, bob):
        client.post("/api/paper/open", headers=headers, json={"option_type": "CE", "strike": contract[0], "quantity": 25})

    PaperExitWorker(on_exits=on_exits).on_chain(chain_with_call_move(contract[0], 1.25), datetime.now(timezone.utc))
    assert len(seen) == 1
    users, statuses = seen[0]
    assert len(users) == 1  # only alice's +10% target was hit
    assert statuses == "EXITED,OPEN"  # the callback ran after the exit was committed


def test_exit_worker_stays_quiet_when_nothing_exits_and_survives_a_failing_callback(client, new_user, contract, chain_with_call_move):
    from src.paper.exit_worker import PaperExitWorker

    calls = []
    _, alice = new_user()
    client.post("/api/paper/open", headers=alice, json={"option_type": "CE", "strike": contract[0], "quantity": 25})
    PaperExitWorker(on_exits=calls.append).on_chain(chain_with_call_move(contract[0], 1.01), datetime.now(timezone.utc))
    assert calls == []

    def boom(_users):
        raise RuntimeError("notification channel down")

    worker = PaperExitWorker(on_exits=boom)
    assert worker.on_chain(chain_with_call_move(contract[0], 1.25), datetime.now(timezone.utc)) == 1  # exit still counted
    assert worker.exits == 1


# -- end to end through the real app (embedded mode) -------------------------------------


@pytest.fixture
def fresh_hub():
    server.rt_hub = Hub()
    server._publisher.new_epoch(int(time.time() * 1000))
    yield
    server.rt_hub = Hub()


@pytest.fixture
def two_sockets(client, new_user, fresh_hub):
    """Alice and Bob, each with an authenticated /ws/v1 connection that has finished its handshake."""
    opened = []

    def connect(headers):
        ctx = client.websocket_connect("/ws/v1")
        ws = ctx.__enter__()
        ws.send_text(auth_frame(headers))
        recv_until(ws, lambda f: f["type"] == "hello")
        opened.append(ctx)
        return ws

    _, alice = new_user()
    _, bob = new_user()
    publish_tick()
    yield alice, connect(alice), bob, connect(bob)
    for ctx in opened:
        ctx.__exit__(None, None, None)


def next_types(ws, until_type):
    """Frame types seen up to and including the first frame of `until_type`."""
    _, seen = recv_until(ws, lambda f: f["type"] == until_type)
    return [f["type"] for f in seen]


def no_notice_for(ws):
    """Prove nothing was queued for this socket: a ping's pong must come back with no notice before it."""
    ws.send_text(json.dumps({"type": "ping"}))
    assert "paper_changed" not in next_types(ws, "pong")


def test_opening_a_position_notifies_only_its_owner(client, two_sockets, contract):
    alice, ws_alice, _bob, ws_bob = two_sockets
    assert client.post("/api/paper/open", headers=alice, json={"option_type": "CE", "strike": contract[0], "quantity": 25}).status_code == 200
    assert "paper_changed" in next_types(ws_alice, "paper_changed")
    no_notice_for(ws_bob)


def test_closing_and_changing_settings_notify(client, two_sockets, contract):
    alice, ws_alice, _bob, _ws_bob = two_sockets
    position = client.post("/api/paper/open", headers=alice, json={"option_type": "PE", "strike": contract[0], "quantity": 25}).json()
    next_types(ws_alice, "paper_changed")
    assert client.post(f"/api/paper/close/{position['id']}", headers=alice).status_code == 200
    next_types(ws_alice, "paper_changed")
    assert client.post("/api/paper/config", headers=alice, json={"take_profit_percent": 20}).status_code == 200
    next_types(ws_alice, "paper_changed")


def test_a_failed_request_does_not_notify(client, two_sockets):
    alice, ws_alice, _bob, _ws_bob = two_sockets
    assert client.post("/api/paper/open", headers=alice, json={"option_type": "CE", "quantity": 0}).status_code == 422
    assert client.post("/api/paper/close/does-not-exist", headers=alice).status_code == 404
    no_notice_for(ws_alice)


def test_an_automatic_exit_notifies_the_owner(client, two_sockets, contract, chain_with_call_move):
    alice, ws_alice, _bob, ws_bob = two_sockets
    client.post("/api/paper/open", headers=alice, json={"option_type": "CE", "strike": contract[0], "quantity": 25})
    next_types(ws_alice, "paper_changed")  # the notice for the open itself
    server._exit_worker.on_chain(chain_with_call_move(contract[0], 1.25), datetime.now(timezone.utc))  # +25% > +10% target
    assert "paper_changed" in next_types(ws_alice, "paper_changed")
    state = client.get("/api/paper/state", headers=alice).json()
    assert state["open_positions"] == [] and state["history"][0]["exit_reason"] == "TAKE_PROFIT"  # what the refetch will see
    no_notice_for(ws_bob)


# -- through Redis (needs a real Redis with working pub/sub) ----------------------------------

URL = os.getenv("TEST_REDIS_URL")


@pytest.mark.skipif(not URL, reason="TEST_REDIS_URL not set")
def test_a_notice_published_to_redis_reaches_the_users_sockets_on_a_gateway():
    import redis

    from src.realtime.redis_bus import RedisSubscriber, publish_user_event

    prefix = f"rt-test-{uuid.uuid4().hex[:8]}:"
    publisher = redis.Redis.from_url(URL, decode_responses=True)

    async def scenario():
        hub = Hub()
        alice, f_alice = make_client(hub, "alice")
        bob, f_bob = make_client(hub, "bob")
        sub = RedisSubscriber(hub, URL, prefix, client_name=f"rt-test-{uuid.uuid4().hex[:6]}")
        sub.start()
        try:
            deadline = time.time() + 5
            while not sub.connected and time.time() < deadline:
                await asyncio.sleep(0.02)
            assert sub.connected
            publish_user_event(publisher, "alice", prefix)
            deadline = time.time() + 5
            while not notices(f_alice) and time.time() < deadline:
                await asyncio.sleep(0.02)
            assert len(notices(f_alice)) == 1
            await asyncio.sleep(0.2)
            assert notices(f_bob) == []
        finally:
            await sub.stop()
            await alice.shutdown()
            await bob.shutdown()

    try:
        asyncio.run(scenario())
    finally:
        publisher.close()
