"""/ws/v1 end to end through the real FastAPI app (embedded mode, no Redis)."""

from __future__ import annotations

import json
import time
import jwt
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

import pytest
from starlette.websockets import WebSocketDisconnect

from src.api import server
from src.realtime.client import TopicMirror
from src.realtime.hub import Hub

TOPICS = ["index:NIFTY", "options:NIFTY", "candles:NIFTY:1m"]


@pytest.fixture(autouse=True)
def fresh_hub():
    """Each test starts with an empty hub, a fresh publisher sequence and its own epoch."""
    server.rt_hub = Hub()
    server._publisher.new_epoch(int(time.time() * 1000))
    yield
    server.rt_hub = Hub()


_receiver = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ws-recv")


def recv_json(ws, timeout=6.0):
    """ws.receive_json() with a deadline. Starlette's test socket blocks forever, which
    would hang the whole suite on a missing frame instead of failing one test."""
    future = _receiver.submit(ws.receive_json)
    try:
        return future.result(timeout)
    except FutureTimeout:
        raise AssertionError(f"timed out after {timeout}s waiting for a frame") from None


def recv_text(ws, timeout=6.0):
    future = _receiver.submit(ws.receive_text)
    try:
        return future.result(timeout)
    except FutureTimeout:
        raise AssertionError(f"timed out after {timeout}s waiting for a frame") from None


def publish_tick():
    """Run one engine tick and push its snapshot through the real publish path."""
    snapshot = server.engine.tick_once()
    assert snapshot is not None
    server._broadcast(snapshot)
    return snapshot


def auth_frame(headers):
    return json.dumps({"type": "auth", "token": headers["Authorization"].split(" ", 1)[1]})


def recv_until(ws, predicate, limit=40):
    """Read frames until one satisfies `predicate`; return (matching frame, all frames)."""
    seen = []
    for _ in range(limit):
        frame = recv_json(ws)
        seen.append(frame)
        if predicate(frame):
            return frame, seen
    raise AssertionError(f"no matching frame in {limit} frames; saw types {[f['type'] for f in seen]}")


def expect_close(ws, code):
    with pytest.raises(WebSocketDisconnect) as info:
        for _ in range(200):
            recv_text(ws)
    assert info.value.code == code


@pytest.fixture
def connect(client, new_user):
    """Open an authenticated /ws/v1 session; returns (websocket context manager, headers)."""
    _, headers = new_user()

    def _open():
        ctx = client.websocket_connect("/ws/v1")
        ws = ctx.__enter__()
        ws.send_text(auth_frame(headers))
        return ctx, ws

    return _open, headers


# -- handshake & subscription ------------------------------------------------------


def test_requires_authentication_when_auth_is_on(client):
    with client.websocket_connect("/ws/v1") as ws:
        ws.send_text(json.dumps({"type": "subscribe", "topics": ["index:NIFTY"]}))  # not an auth frame
        expect_close(ws, 4401)
    with client.websocket_connect("/ws/v1") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "garbage"}))
        expect_close(ws, 4401)


def test_hello_subscribe_snapshots_then_patches_and_the_client_converges(client, connect):
    open_ws, _ = connect
    publish_tick()
    ctx, ws = open_ws()
    try:
        hello, _ = recv_until(ws, lambda f: f["type"] == "hello")
        assert hello["v"] == 1 and hello["max_topics"] >= 1
        ws.send_text(json.dumps({"type": "subscribe", "topics": TOPICS}))
        mirror = TopicMirror()
        got_snapshots = set()
        while len(got_snapshots) < len(TOPICS):
            frame = recv_json(ws)
            if frame["type"] == "snapshot":
                got_snapshots.add(frame["topic"])
                assert mirror.apply(frame) == "ok"
        assert got_snapshots == set(TOPICS)

        patches = 0
        for _ in range(8):
            publish_tick()
            time.sleep(0.02)
        deadline = time.time() + 5
        while time.time() < deadline:
            frame = recv_json(ws)
            if frame["type"] in ("snapshot", "patch"):
                patches += frame["type"] == "patch"
                assert mirror.apply(frame) != "resync", f"lost sync at {frame['topic']} seq {frame['seq']}"
            if all(mirror.version(t) == (server.rt_hub.states[t].epoch, server.rt_hub.states[t].seq) for t in TOPICS):
                break
        for topic in TOPICS:
            assert mirror.payload(topic) == server.rt_hub.latest(topic), topic
        assert patches > 0  # steady-state traffic really is incremental
    finally:
        ctx.__exit__(None, None, None)


def test_no_frame_ever_carries_a_users_paper_book(client, connect, contract):
    open_ws, headers = connect
    client.post("/api/paper/open", headers=headers, json={"option_type": "CE", "strike": contract[0], "quantity": 25})
    publish_tick()
    ctx, ws = open_ws()
    try:
        ws.send_text(json.dumps({"type": "subscribe", "topics": TOPICS + ["rrg:NIFTY"]}))
        publish_tick()
        seen = []
        deadline = time.time() + 3
        while time.time() < deadline and len(seen) < 8:
            seen.append(json.dumps(recv_json(ws)))
        assert seen and not any("paper_trading" in frame for frame in seen)
    finally:
        ctx.__exit__(None, None, None)


def test_bad_topics_are_reported_individually_and_good_ones_still_work(client, connect):
    open_ws, _ = connect
    publish_tick()
    ctx, ws = open_ws()
    try:
        ws.send_text(json.dumps({"type": "subscribe", "topics": ["index:NIFTY", "options:*", "paper:NIFTY", "index:RELIANCE"]}))
        errors, subscribed, snaps = [], None, []
        deadline = time.time() + 3
        while time.time() < deadline and not (subscribed and snaps):
            frame = recv_json(ws)
            if frame["type"] == "error":
                errors.append(frame)
            elif frame["type"] == "subscribed":
                subscribed = frame
            elif frame["type"] == "snapshot":
                snaps.append(frame["topic"])
        assert [e["code"] for e in errors] == ["bad_topic"] * 3
        assert subscribed["topics"] == ["index:NIFTY"] and snaps == ["index:NIFTY"]
    finally:
        ctx.__exit__(None, None, None)


def test_subscribing_before_data_exists_delivers_the_snapshot_once_it_does(client, connect):
    open_ws, _ = connect
    ctx, ws = open_ws()  # hub is empty: nothing published yet
    try:
        ws.send_text(json.dumps({"type": "subscribe", "topics": ["index:NIFTY"]}))
        recv_until(ws, lambda f: f["type"] == "subscribed")
        publish_tick()
        frame, _ = recv_until(ws, lambda f: f["type"] == "snapshot")
        assert frame["topic"] == "index:NIFTY"
    finally:
        ctx.__exit__(None, None, None)


def test_unsubscribe_stops_the_flow(client, connect):
    open_ws, _ = connect
    publish_tick()
    ctx, ws = open_ws()
    try:
        ws.send_text(json.dumps({"type": "subscribe", "topics": ["index:NIFTY"]}))
        recv_until(ws, lambda f: f["type"] == "snapshot")
        ws.send_text(json.dumps({"type": "unsubscribe", "topics": ["index:NIFTY"]}))
        recv_until(ws, lambda f: f["type"] == "unsubscribed")
        publish_tick()
        ws.send_text(json.dumps({"type": "ping"}))
        frames = []
        frame, frames = recv_until(ws, lambda f: f["type"] == "pong")
        assert not [f for f in frames if f["type"] in ("snapshot", "patch")]
    finally:
        ctx.__exit__(None, None, None)


def test_resync_returns_a_fresh_snapshot_and_requires_a_subscription(client, connect):
    open_ws, _ = connect
    publish_tick()
    ctx, ws = open_ws()
    try:
        ws.send_text(json.dumps({"type": "resync", "topic": "index:NIFTY"}))
        error, _ = recv_until(ws, lambda f: f["type"] == "error")
        assert error["code"] == "not_subscribed"
        ws.send_text(json.dumps({"type": "subscribe", "topics": ["index:NIFTY"]}))
        recv_until(ws, lambda f: f["type"] == "snapshot")
        publish_tick()
        ws.send_text(json.dumps({"type": "resync", "topic": "index:NIFTY"}))
        frame, _ = recv_until(ws, lambda f: f["type"] == "snapshot" and f["seq"] >= 2)
        assert frame["topic"] == "index:NIFTY"
    finally:
        ctx.__exit__(None, None, None)


# -- hostile input -----------------------------------------------------------------


def test_malformed_frames_get_errors_but_do_not_drop_the_connection(client, connect):
    open_ws, _ = connect
    ctx, ws = open_ws()
    try:
        for raw, code in [("not json", "bad_json"), ("[1,2]", "bad_frame"), ('"hi"', "bad_frame"),
                          (json.dumps({"type": "teleport"}), "unknown_type"),
                          (json.dumps({"type": "subscribe", "topics": "index:NIFTY"}), "bad_topic"),
                          (json.dumps({"type": "subscribe", "topics": [1, None, {}]}), "bad_topic")]:
            ws.send_text(raw)
            frame, _ = recv_until(ws, lambda f: f["type"] == "error")
            assert frame["code"] == code, raw
        ws.send_text(json.dumps({"type": "ping"}))
        recv_until(ws, lambda f: f["type"] == "pong")  # still alive
    finally:
        ctx.__exit__(None, None, None)


def test_oversized_frames_close_the_connection(client, connect):
    open_ws, _ = connect
    ctx, ws = open_ws()
    try:
        ws.send_text(json.dumps({"type": "subscribe", "topics": ["index:NIFTY"], "pad": "x" * 10_000}))
        expect_close(ws, 1009)
    finally:
        ctx.__exit__(None, None, None)


def test_a_flood_of_frames_is_rate_limited_then_disconnected(client, connect):
    open_ws, _ = connect
    ctx, ws = open_ws()
    try:
        sent = 0
        with pytest.raises(WebSocketDisconnect):
            for _ in range(400):
                ws.send_text(json.dumps({"type": "ping"}))
                sent += 1
            for _ in range(500):
                recv_text(ws)
        from src.realtime import metrics

        assert metrics.inbound_rejected.labels("rate_limited")._value.get() > 0
    finally:
        ctx.__exit__(None, None, None)


def test_topics_per_client_are_capped(client, connect, monkeypatch):
    from src.realtime import hub as hub_module

    monkeypatch.setattr(hub_module, "MAX_TOPICS_PER_CLIENT", 2)
    open_ws, _ = connect
    ctx, ws = open_ws()
    try:
        ws.send_text(json.dumps({"type": "subscribe", "topics": TOPICS}))
        error, _ = recv_until(ws, lambda f: f["type"] == "error")
        assert error["code"] == "too_many_topics"
    finally:
        ctx.__exit__(None, None, None)


def test_connections_per_user_are_capped(client, new_user, monkeypatch):
    monkeypatch.setenv("WS_MAX_CONNECTIONS_PER_USER", "2")
    _, headers = new_user()
    with client.websocket_connect("/ws/v1") as a, client.websocket_connect("/ws/v1") as b, client.websocket_connect("/ws/v1") as c:
        for ws in (a, b):
            ws.send_text(auth_frame(headers))
            recv_until(ws, lambda f: f["type"] == "hello")
        c.send_text(auth_frame(headers))
        expect_close(c, 4429)


# -- token lifetime ----------------------------------------------------------------


def test_the_socket_closes_when_its_access_token_expires(client, new_user, monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_TTL_MINUTES", "0.1")  # 6 s: enough for a slow host to connect first
    _, headers = new_user()
    with client.websocket_connect("/ws/v1") as ws:
        ws.send_text(auth_frame(headers))
        recv_until(ws, lambda f: f["type"] == "hello")
        expires_at = jwt.decode(headers["Authorization"].split(" ", 1)[1], options={"verify_signature": False})["exp"]
        expect_close(ws, 4401)
        closed_at = time.time()
        # Never before the token's own expiry (a slow machine may have used up much of its life already),
        # and promptly after it.
        assert closed_at >= expires_at - 0.25, "closed before the token expired"
        assert closed_at - expires_at < 6


def test_a_fresh_token_renews_the_session_in_band(client, new_user, monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_TTL_MINUTES", "0.1")  # 6 s: enough for a slow host to connect first
    email, headers = new_user()
    original_exp = jwt.decode(headers["Authorization"].split(" ", 1)[1], options={"verify_signature": False})["exp"]
    with client.websocket_connect("/ws/v1") as ws:
        ws.send_text(auth_frame(headers))
        recv_until(ws, lambda f: f["type"] == "hello")
        assert time.time() < original_exp - 0.5, "host too slow for this test's token lifetime"
        monkeypatch.setenv("ACCESS_TOKEN_TTL_MINUTES", "15")
        fresh = client.post("/api/auth/login", json={"email": email, "password": "correct horse battery"}).json()
        assert time.time() < original_exp - 0.2, "host too slow for this test's token lifetime"
        ws.send_text(json.dumps({"type": "auth", "token": fresh["access_token"]}))
        ok, _ = recv_until(ws, lambda f: f["type"] == "auth_ok")
        assert ok["exp"] > time.time() * 1000 + 60_000
        time.sleep(max(0.0, original_exp - time.time()) + 0.8)  # well past the ORIGINAL token's expiry
        ws.send_text(json.dumps({"type": "ping"}))
        recv_until(ws, lambda f: f["type"] == "pong")


def test_renewing_with_someone_elses_token_is_refused(client, new_user):
    _, mine = new_user()
    _, theirs = new_user()
    with client.websocket_connect("/ws/v1") as ws:
        ws.send_text(auth_frame(mine))
        recv_until(ws, lambda f: f["type"] == "hello")
        ws.send_text(auth_frame(theirs))
        expect_close(ws, 4401)


# -- metrics & disabled mode -------------------------------------------------------


def test_metrics_are_not_world_readable_when_auth_is_on(client, monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    assert client.get("/metrics").status_code == 404
    monkeypatch.setenv("METRICS_TOKEN", "s3cret-metrics-token")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer s3cret-metrics-token"})
    assert ok.status_code == 200 and "rt_ws_clients" in ok.text and "rt_updates_total" in ok.text


def test_v1_works_without_auth_in_disabled_mode(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_MODE", "disabled")
    monkeypatch.setattr(server.engine, "start", lambda: None)
    with TestClient(server.app) as plain:
        publish_tick()
        with plain.websocket_connect("/ws/v1") as ws:
            recv_until(ws, lambda f: f["type"] == "hello")
            ws.send_text(json.dumps({"type": "subscribe", "topics": ["index:NIFTY"]}))
            frame, _ = recv_until(ws, lambda f: f["type"] == "snapshot")
            assert frame["topic"] == "index:NIFTY"
        assert TestClient(server.app).get("/metrics").status_code == 200
