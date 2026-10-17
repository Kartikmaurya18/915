"""Batch frames: one websocket message per tick for clients that ask for it, unchanged behaviour for the rest."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from src.api import server
from src.realtime import hub as hub_module
from src.realtime import protocol
from src.realtime.client import TopicMirror
from src.realtime.hub import Hub, TopicUpdate

TOPICS = ["index:NIFTY", "options:NIFTY", "candles:NIFTY:1m"]


def update(topic, seq, payload=None, epoch=1):
    return TopicUpdate(topic, epoch, seq, ts=1000 + seq, pub=int(time.time() * 1000), payload=payload if payload is not None else {"n": seq})


def make_client(hub, user_id="u", batch=False):
    messages: list[dict] = []

    async def send(frame: str):
        messages.append(json.loads(frame))

    async def close(code, reason):
        pass

    client = hub.add_client(send, close, user_id)
    client.batch = batch
    return client, messages


def tick(hub, seq, topics=TOPICS):
    for topic in topics:  # the topics of one tick arrive as separate updates, a moment apart
        hub.apply_update(update(topic, seq, {"n": seq, "pad": "x" * 200}))


def run(coro):
    return asyncio.run(coro)


def data_messages(messages):
    return [m for m in messages if m["type"] in ("snapshot", "patch", "batch")]


# -- protocol ------------------------------------------------------------------------------------


def test_batch_message_is_valid_json_with_the_inner_frames_intact():
    inner = [protocol.snapshot_message("a:X", 1, 1, 1, 1, {"k": [1, 2]}), protocol.patch_message("b:X", 1, 2, 1, 1, 1, ["d", {}, []])]
    parsed = json.loads(protocol.batch_message(inner))
    assert parsed["type"] == "batch" and parsed["v"] == 1
    assert parsed["frames"] == [json.loads(f) for f in inner]  # byte-for-byte the same frames, no re-encoding


def test_an_empty_or_single_batch_is_still_valid():
    assert json.loads(protocol.batch_message([]))["frames"] == []
    assert len(json.loads(protocol.batch_message([protocol.control_message("pong")]))["frames"]) == 1


# -- the hub ------------------------------------------------------------------------------------------


def test_a_batching_client_gets_all_topics_of_a_tick_in_one_message():
    async def scenario():
        hub = Hub()
        client, messages = make_client(hub, batch=True)
        client.subscribe(TOPICS)
        tick(hub, 1)
        await asyncio.sleep(0.1)
        data = data_messages(messages)
        assert len(data) == 1 and data[0]["type"] == "batch"
        assert sorted(f["topic"] for f in data[0]["frames"]) == sorted(TOPICS)
        assert {f["type"] for f in data[0]["frames"]} == {"snapshot"}  # first delivery is state
        await client.shutdown()

    run(scenario())


def test_the_next_ticks_are_patches_inside_batches_and_state_stays_in_step():
    async def scenario():
        hub = Hub()
        client, messages = make_client(hub, batch=True)
        client.subscribe(TOPICS)
        tick(hub, 1)
        await asyncio.sleep(0.1)
        for seq in (2, 3, 4):
            tick(hub, seq)
            await asyncio.sleep(0.1)
        batches = [m for m in messages if m["type"] == "batch"]
        assert len(batches) == 4  # one message per tick
        later = [f for b in batches[1:] for f in b["frames"]]
        assert {f["type"] for f in later} == {"patch"}  # the sent-state bookkeeping works across batches
        mirror = TopicMirror()
        for b in batches:
            for frame in b["frames"]:
                assert mirror.apply(frame) == "ok"
        for topic in TOPICS:
            assert mirror.payload(topic) == hub.latest(topic)
        await client.shutdown()

    run(scenario())


def test_a_client_that_did_not_opt_in_never_receives_a_batch():
    async def scenario():
        hub = Hub()
        client, messages = make_client(hub, batch=False)
        client.subscribe(TOPICS)
        tick(hub, 1)
        await asyncio.sleep(0.1)
        data = data_messages(messages)
        assert [m["type"] for m in data] == ["snapshot"] * 3  # as before: one message per topic
        await client.shutdown()

    run(scenario())


def test_a_single_dirty_topic_is_sent_plain_not_wrapped():
    async def scenario():
        hub = Hub()
        client, messages = make_client(hub, batch=True)
        client.subscribe(["index:NIFTY"])
        tick(hub, 1, ["index:NIFTY"])
        await asyncio.sleep(0.1)
        assert [m["type"] for m in data_messages(messages)] == ["snapshot"]
        await client.shutdown()

    run(scenario())


def test_batching_and_plain_clients_see_identical_state():
    async def scenario():
        hub = Hub()
        batching, b_msgs = make_client(hub, "a", batch=True)
        plain, p_msgs = make_client(hub, "b", batch=False)
        for c in (batching, plain):
            c.subscribe(TOPICS)
        for seq in range(1, 6):
            tick(hub, seq)
            await asyncio.sleep(0.08)

        def final_state(messages):
            mirror = TopicMirror()
            for m in messages:
                frames = m["frames"] if m["type"] == "batch" else [m]
                for f in frames:
                    if f["type"] in ("snapshot", "patch"):
                        assert mirror.apply(f) == "ok"
            return {t: mirror.payload(t) for t in TOPICS}

        assert final_state(b_msgs) == final_state(p_msgs) == {t: hub.latest(t) for t in TOPICS}
        assert len([m for m in b_msgs if m["type"] == "batch"]) < len([m for m in p_msgs if m["type"] in ("snapshot", "patch")])
        await batching.shutdown()
        await plain.shutdown()

    run(scenario())


def test_a_batching_client_that_falls_behind_still_skips_versions_and_converges():
    async def scenario():
        hub = Hub()
        client, messages = make_client(hub, batch=True)
        client.subscribe(TOPICS)
        for seq in range(1, 30):  # many ticks before the client task gets to run
            tick(hub, seq)
        await asyncio.sleep(0.15)
        frames = [f for m in messages if m["type"] == "batch" for f in m["frames"]]
        assert len(frames) <= len(TOPICS) * 2  # coalesced: it did not replay every version
        mirror = TopicMirror()
        for f in frames:
            assert mirror.apply(f) != "resync"
        assert {t: mirror.payload(t) for t in TOPICS} == {t: hub.latest(t) for t in TOPICS}
        await client.shutdown()

    run(scenario())


def test_the_batch_window_can_be_disabled(monkeypatch):
    monkeypatch.setattr(hub_module, "BATCH_WINDOW_SECONDS", 0.0)

    async def scenario():
        hub = Hub()
        client, messages = make_client(hub, batch=True)
        client.subscribe(TOPICS)
        tick(hub, 1)
        await asyncio.sleep(0.1)
        assert data_messages(messages)  # still delivers; only the batching opportunity shrinks
        await client.shutdown()

    run(scenario())


def test_paper_notices_and_control_frames_stay_outside_batches():
    async def scenario():
        hub = Hub()
        client, messages = make_client(hub, user_id="alice", batch=True)
        client.subscribe(TOPICS)
        tick(hub, 1)
        hub.notify_user("alice")
        await asyncio.sleep(0.1)
        types = [m["type"] for m in messages]
        assert "paper_changed" in types and "batch" in types
        assert all(f["type"] != "paper_changed" for m in messages if m["type"] == "batch" for f in m["frames"])
        await client.shutdown()

    run(scenario())


# -- through the real app ---------------------------------------------------------------------


@pytest.fixture
def fresh_hub():
    server.rt_hub = Hub()
    server._publisher.new_epoch(int(time.time() * 1000))
    yield
    server.rt_hub = Hub()


def test_over_a_real_websocket_only_opted_in_clients_get_batches(client, new_user, fresh_hub):
    from tests.test_rt_ws import auth_frame, publish_tick, recv_json, recv_until

    _, headers = new_user()
    publish_tick()
    for opt_in in (True, False):
        with client.websocket_connect("/ws/v1") as ws:
            ws.send_text(auth_frame(headers))
            recv_until(ws, lambda f: f["type"] == "hello")
            ws.send_text(json.dumps({"type": "subscribe", "topics": TOPICS, **({"batch": True} if opt_in else {})}))
            seen_topics, kinds = set(), set()
            mirror = TopicMirror()
            while len(seen_topics) < len(TOPICS):
                message = recv_json(ws)
                kinds.add(message["type"])
                for frame in (message["frames"] if message["type"] == "batch" else [message]):
                    if frame["type"] == "snapshot":
                        seen_topics.add(frame["topic"])
                        assert mirror.apply(frame) == "ok"
            assert ("batch" in kinds) == opt_in
            for topic in TOPICS:
                assert mirror.payload(topic) == server.rt_hub.latest(topic)


def test_batch_must_be_a_real_boolean_to_opt_in(client, new_user, fresh_hub):
    from tests.test_rt_ws import auth_frame, publish_tick, recv_json, recv_until

    _, headers = new_user()
    publish_tick()
    with client.websocket_connect("/ws/v1") as ws:
        ws.send_text(auth_frame(headers))
        recv_until(ws, lambda f: f["type"] == "hello")
        ws.send_text(json.dumps({"type": "subscribe", "topics": TOPICS, "batch": "yes"}))
        kinds, topics = set(), set()
        while len(topics) < len(TOPICS):  # the three snapshots arrive; none of them may be wrapped
            message = recv_json(ws)
            kinds.add(message["type"])
            if message["type"] == "snapshot":
                topics.add(message["topic"])
        assert "batch" not in kinds
