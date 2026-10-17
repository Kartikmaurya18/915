"""The hub's delivery guarantees, with no network: fake sockets whose speed we control."""

from __future__ import annotations

import asyncio
import copy
import json
import random
import time

import pytest

from src.realtime import hub as hub_module
from src.realtime.client import TopicMirror
from src.realtime.hub import Hub, TopicUpdate

TOPIC = "options:NIFTY"


def rows(n=40, bump=0.0):
    return {"expiry": "22-Oct-2026", "strikes": [{"strike": 24000.0 + 50 * i, "ltp": 100.0 + i + bump, "oi": 1000 * i} for i in range(n)]}


def upd(seq, payload, epoch=1, topic=TOPIC):
    return TopicUpdate(topic, epoch, seq, ts=1_000 + seq, pub=int(time.time() * 1000), payload=payload)


class FakeSocket:
    """A client socket with a controllable write speed, recording every frame."""

    def __init__(self, delay=0.0, stall=False):
        self.delay, self.stall = delay, stall
        self.frames: list[dict] = []
        self.times: list[float] = []
        self.closed: tuple[int, str] | None = None
        self.release = asyncio.Event()

    async def send(self, frame: str) -> None:
        if self.stall:
            await self.release.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.frames.append(json.loads(frame))
        self.times.append(time.perf_counter())

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)

    def attach(self, hub: Hub, user=None):
        return hub.add_client(self.send, self.close, user)


async def settle(delay=0.05):
    await asyncio.sleep(delay)


def mirror_of(sock: FakeSocket) -> TopicMirror:
    mirror = TopicMirror()
    for frame in sock.frames:
        if frame["type"] in ("snapshot", "patch"):
            assert mirror.apply(frame) != "resync", f"client lost sync on {frame['type']} seq={frame['seq']}"
    return mirror


def run(coro):
    return asyncio.run(coro)


# -- basics ------------------------------------------------------------------------


def test_subscriber_gets_a_snapshot_then_patches_and_converges():
    async def scenario():
        hub, sock = Hub(), FakeSocket()
        client = sock.attach(hub)
        hub.apply_update(upd(1, rows()))
        client.subscribe([TOPIC])
        await settle()
        assert [f["type"] for f in sock.frames] == ["snapshot"]
        hub.apply_update(upd(2, rows(bump=0.5)))
        await settle()
        assert [f["type"] for f in sock.frames] == ["snapshot", "patch"]
        assert mirror_of(sock).payload(TOPIC) == hub.latest(TOPIC)
        await hub.close_all()

    run(scenario())


def test_patch_frames_are_smaller_than_the_snapshot_they_replace():
    async def scenario():
        hub, sock = Hub(), FakeSocket()
        client = sock.attach(hub)
        client.subscribe([TOPIC])
        hub.apply_update(upd(1, rows(80)))
        await settle()  # let the client receive v1, otherwise v1->v2 is correctly coalesced into one snapshot
        changed = rows(80)
        changed["strikes"][5]["ltp"] += 1
        hub.apply_update(upd(2, changed))
        await settle()
        sizes = {f["type"]: len(json.dumps(f)) for f in sock.frames}
        assert sizes["patch"] * 10 < sizes["snapshot"]
        await hub.close_all()

    run(scenario())


def test_a_patch_larger_than_the_snapshot_is_never_sent():
    async def scenario():
        hub, sock = Hub(), FakeSocket()
        client = sock.attach(hub)
        client.subscribe([TOPIC])
        hub.apply_update(upd(1, {"a": 1}))
        hub.apply_update(upd(2, {"a": 2}))  # tiny payload: the patch envelope is no smaller
        await settle()
        assert all(f["type"] == "snapshot" for f in sock.frames)
        assert mirror_of(sock).payload(TOPIC) == {"a": 2}
        await hub.close_all()

    run(scenario())


def test_unsubscribed_and_unknown_topics_receive_nothing():
    async def scenario():
        hub, sock = Hub(), FakeSocket()
        client = sock.attach(hub)
        client.subscribe([TOPIC])
        hub.apply_update(upd(1, rows()))
        hub.apply_update(upd(1, {"x": 1}, topic="index:NIFTY"))
        await settle()
        assert {f["topic"] for f in sock.frames} == {TOPIC}
        client.unsubscribe([TOPIC])
        hub.apply_update(upd(2, rows(bump=1)))
        await settle()
        assert len(sock.frames) == 1
        await hub.close_all()

    run(scenario())


def test_a_late_subscriber_starts_from_a_snapshot_not_a_patch():
    async def scenario():
        hub = Hub()
        for seq in range(1, 6):
            hub.apply_update(upd(seq, rows(bump=seq)))
        sock = FakeSocket()
        sock.attach(hub).subscribe([TOPIC])
        await settle()
        assert [f["type"] for f in sock.frames] == ["snapshot"] and sock.frames[0]["seq"] == 5
        await hub.close_all()

    run(scenario())


# -- ordering: duplicates, reordering, new leaders ---------------------------------


def test_duplicate_and_out_of_order_updates_are_ignored():
    async def scenario():
        hub = Hub()
        assert hub.apply_update(upd(5, rows(bump=5))) == "applied"
        assert hub.apply_update(upd(5, rows(bump=99))) == "duplicate"
        assert hub.apply_update(upd(4, rows(bump=4))) == "out_of_order"
        assert hub.apply_update(upd(6, rows(bump=6))) == "applied"
        assert hub.states[TOPIC].seq == 6
        assert hub.latest(TOPIC) == rows(bump=6)

    run(scenario())


def test_a_new_epoch_wins_even_with_a_lower_seq_and_forces_a_snapshot():
    async def scenario():
        hub, sock = Hub(), FakeSocket()
        client = sock.attach(hub)
        client.subscribe([TOPIC])
        def one_row_changed(n):
            body = rows(40)
            body["strikes"][3]["ltp"] += n
            return body

        hub.apply_update(upd(50, one_row_changed(1), epoch=1))
        await settle()
        hub.apply_update(upd(51, one_row_changed(2), epoch=1))
        await settle()
        assert sock.frames[-1]["type"] == "patch"
        assert hub.apply_update(upd(1, rows(bump=3), epoch=2)) == "applied"  # new leader restarts seq
        assert hub.apply_update(upd(52, rows(bump=9), epoch=1)) == "out_of_order"  # the old leader is stale
        await settle()
        last = sock.frames[-1]
        assert last["type"] == "snapshot" and (last["epoch"], last["seq"]) == (2, 1)
        assert mirror_of(sock).payload(TOPIC) == rows(bump=3)
        await hub.close_all()

    run(scenario())


def test_client_detects_a_gap_and_recovers_with_resync():
    """A client that misses a frame must notice (the next patch's `base` is not what it
    holds), refuse to apply it, and recover via resync - never silently diverge."""
    async def scenario():
        hub, sock = Hub(), FakeSocket()
        client = sock.attach(hub)
        client.subscribe([TOPIC])

        def version(n):
            body = rows(80)
            body["strikes"][n]["ltp"] += n
            return body

        hub.apply_update(upd(1, version(0)))
        await settle()
        mirror = TopicMirror()
        mirror.apply(sock.frames[0])  # the client holds seq 1

        hub.apply_update(upd(2, version(2)))
        missed = json.loads(hub.states[TOPIC].patch_json)  # seq 2's patch: the "lost" frame
        hub.apply_update(upd(3, version(3)))
        arrives = json.loads(hub.states[TOPIC].patch_json)  # seq 3's patch, based on seq 2
        assert missed["type"] == arrives["type"] == "patch" and arrives["base"] == 2

        assert mirror.apply(arrives) == "resync"  # detected the gap ...
        assert mirror.version(TOPIC) == (1, 1) and mirror.resyncs == 1  # ... and did NOT apply it
        before = len(sock.frames)
        await settle()
        client.request_resync(TOPIC)
        await settle()
        assert sock.frames[-1]["type"] == "snapshot" and len(sock.frames) > before
        mirror.apply(sock.frames[-1])
        assert mirror.payload(TOPIC) == hub.latest(TOPIC)
        await hub.close_all()

    run(scenario())


# -- isolation: the acceptance criteria --------------------------------------------


def test_a_slow_client_never_delays_a_healthy_one(monkeypatch):
    monkeypatch.setattr(hub_module, "SEND_TIMEOUT_SECONDS", 30)

    async def scenario():
        hub = Hub()
        fast, slow = FakeSocket(), FakeSocket(delay=0.25)
        fc, sc = fast.attach(hub), slow.attach(hub)
        fc.subscribe([TOPIC])
        sc.subscribe([TOPIC])
        hub.apply_update(upd(1, rows()))
        await settle()

        started, ingest_durations = time.perf_counter(), []
        for seq in range(2, 62):
            t = time.perf_counter()
            hub.apply_update(upd(seq, rows(bump=seq)))
            ingest_durations.append(time.perf_counter() - t)
            await asyncio.sleep(0.005)
        await settle(0.1)
        elapsed = time.perf_counter() - started

        # 1. ingest never waited on the slow client. Structurally: apply_update is a plain
        #    function (it cannot await anyone); empirically: its cost stays tiny even though
        #    one client needs 250 ms per frame (generous bound: Windows timers jitter).
        assert not asyncio.iscoroutinefunction(hub.apply_update)
        ingest_durations.sort()
        assert ingest_durations[int(len(ingest_durations) * 0.9)] < 0.01, ingest_durations[-5:]
        # One call can still be stalled by the whole process being descheduled (GC, a loaded CI or laptop: a 0.7 s
        # stall was seen under a full suite plus Docker). Waiting on the slow client would instead push EVERY call past
        # 0.25 s, which the 90th-percentile bound above already rules out; this only catches the pathological case.
        assert ingest_durations[-1] < 5.0
        # 2. the healthy client got every version, in order, promptly
        fast_seqs = [f["seq"] for f in fast.frames]
        assert fast_seqs == sorted(fast_seqs) and fast_seqs[-1] == 61 and len(fast_seqs) == 61
        assert fast.times[-1] - started < elapsed + 0.05
        # 3. the slow client skipped versions instead of queueing them
        assert len(slow.frames) < 15
        assert sc.stats["coalesced"] > 0 or any(f["type"] == "snapshot" for f in slow.frames[1:])
        # 4. and it still converges once it catches up
        await settle(0.6)
        assert mirror_of(slow).payload(TOPIC) == hub.latest(TOPIC)
        assert mirror_of(fast).payload(TOPIC) == hub.latest(TOPIC)
        await hub.close_all()

    run(scenario())


def test_a_stalled_client_is_disconnected_and_the_rest_are_unaffected(monkeypatch):
    monkeypatch.setattr(hub_module, "SEND_TIMEOUT_SECONDS", 0.2)

    async def scenario():
        hub = Hub()
        healthy, stalled = FakeSocket(), FakeSocket(stall=True)
        hc, sc = healthy.attach(hub), stalled.attach(hub)
        hc.subscribe([TOPIC])
        sc.subscribe([TOPIC])
        for seq in range(1, 30):
            hub.apply_update(upd(seq, rows(bump=seq)))
            await asyncio.sleep(0.01)
        await settle(0.5)
        assert stalled.closed is not None and stalled.closed[1] == "slow_consumer"
        assert sc not in hub.clients and hc in hub.clients
        assert healthy.frames[-1]["seq"] == 29
        # bounded memory: whatever the stalled client was owed is capped, not a backlog of 29
        assert len(sc._dirty) <= 1 and len(sc._control) == 0
        await hub.close_all()

    run(scenario())


def test_fast_producer_cannot_grow_a_slow_clients_memory(monkeypatch):
    monkeypatch.setattr(hub_module, "SEND_TIMEOUT_SECONDS", 30)

    async def scenario():
        hub, slow = Hub(), FakeSocket(stall=True)
        client = slow.attach(hub)
        client.subscribe([TOPIC, "index:NIFTY", "candles:NIFTY:1m"])
        for seq in range(1, 5001):
            for topic in (TOPIC, "index:NIFTY", "candles:NIFTY:1m"):
                hub.apply_update(upd(seq, {"n": seq, "pad": "x" * 200}, topic=topic))
        await settle(0.05)
        assert len(client._dirty) <= 3 and len(client._control) == 0  # one slot per topic, however many updates
        assert client.stats["coalesced"] > 10_000
        slow.release.set()
        await settle(0.2)
        assert mirror_of(slow).payload(TOPIC)["n"] == 5000
        await hub.close_all()

    run(scenario())


def test_many_clients_receive_the_same_pre_serialised_frame():
    async def scenario():
        hub = Hub()
        socks = [FakeSocket() for _ in range(200)]
        for s in socks:
            s.attach(hub).subscribe([TOPIC])
        hub.apply_update(upd(1, rows(80)))
        changed = rows(80)
        changed["strikes"][0]["ltp"] += 1
        before = hub.states[TOPIC]
        hub.apply_update(upd(2, changed))
        await settle(0.3)
        state = hub.states[TOPIC]
        assert state is not before and state.patch_json is not None
        assert all(s.frames[-1]["seq"] == 2 for s in socks)  # 200 clients, one serialisation of the frame
        await hub.close_all()

    run(scenario())


# -- limits & housekeeping ---------------------------------------------------------


def test_control_queue_overflow_disconnects_the_client(monkeypatch):
    monkeypatch.setattr(hub_module, "CONTROL_QUEUE_MAX", 5)
    monkeypatch.setattr(hub_module, "SEND_TIMEOUT_SECONDS", 30)

    async def scenario():
        hub, sock = Hub(), FakeSocket(stall=True)
        client = sock.attach(hub)
        for i in range(50):
            client.queue_control(json.dumps({"type": "error", "i": i}))
        await settle()
        assert client.closed and client.close_reason == "control_overflow" and client not in hub.clients

    run(scenario())


def test_topic_limit_per_client(monkeypatch):
    monkeypatch.setattr(hub_module, "MAX_TOPICS_PER_CLIENT", 2)

    async def scenario():
        hub, sock = Hub(), FakeSocket()
        client = sock.attach(hub)
        accepted = client.subscribe(["index:NIFTY", TOPIC, "rrg:NIFTY"])
        await settle()
        assert accepted == ["index:NIFTY", TOPIC]
        assert any(f.get("code") == "too_many_topics" for f in sock.frames)
        await hub.close_all()

    run(scenario())


def test_admission_limits():
    async def scenario():
        hub = Hub(max_clients=3, max_clients_per_user=2)
        socks = [FakeSocket() for _ in range(4)]
        assert hub.admit("u1") is None
        socks[0].attach(hub, "u1")
        socks[1].attach(hub, "u1")
        assert hub.admit("u1") == "too_many_connections"
        assert hub.admit("u2") is None
        socks[2].attach(hub, "u2")
        assert hub.admit("u3") == "server_at_capacity"
        await hub.close_all()
        assert hub.admit("u1") is None

    run(scenario())


def test_heartbeat_reports_feed_age_so_clients_can_see_a_dead_feed(monkeypatch):
    monkeypatch.setattr(hub_module, "HEARTBEAT_SECONDS", 0.05)

    async def scenario():
        hub, sock = Hub(), FakeSocket()
        sock.attach(hub)
        await asyncio.sleep(0.12)
        assert sock.frames and sock.frames[0]["type"] == "heartbeat" and sock.frames[0]["feed_age_ms"] is None
        hub.apply_update(upd(1, rows()))
        await asyncio.sleep(0.2)
        ages = [f["feed_age_ms"] for f in sock.frames if f["type"] == "heartbeat" and f["feed_age_ms"] is not None]
        assert ages and ages == sorted(ages) and ages[-1] >= 100  # no updates -> the reported age keeps growing
        await hub.close_all()

    run(scenario())


def test_disconnect_cleans_up_every_reference():
    async def scenario():
        hub, sock = Hub(), FakeSocket()
        client = sock.attach(hub, "u1")
        client.subscribe([TOPIC, "index:NIFTY"])
        await client.shutdown()
        assert not hub.clients and hub.subscription_count() == 0 and hub.admit("u1") is None
        hub.apply_update(upd(1, rows()))  # nobody to notify; must not raise

    run(scenario())


# -- convergence under chaos -------------------------------------------------------


def test_clients_always_converge_whatever_the_schedule():
    """Random update bursts, random per-client speeds and random mid-stream (un)subscribes:
    once things quiesce every client mirrors the hub exactly."""
    async def scenario():
        rng = random.Random(7)
        hub = Hub()
        topics = [TOPIC, "index:NIFTY", "candles:NIFTY:1m"]
        socks = [FakeSocket(delay=rng.choice([0, 0, 0.01, 0.03])) for _ in range(12)]
        clients = [s.attach(hub) for s in socks]
        for c in clients:
            c.subscribe(rng.sample(topics, rng.randint(1, 3)))
        seq = {t: 0 for t in topics}
        body = {t: rows(30) for t in topics}
        for step in range(120):
            topic = rng.choice(topics)
            seq[topic] += 1
            body[topic] = copy.deepcopy(body[topic])
            for r in rng.sample(body[topic]["strikes"], 3):
                r["ltp"] = round(r["ltp"] + rng.random(), 3)
            hub.apply_update(upd(seq[topic], body[topic], topic=topic))
            if step % 17 == 0:
                rng.choice(clients).request_resync(rng.choice(topics))
            await asyncio.sleep(rng.choice([0, 0.001, 0.004]))
        await settle(1.0)
        for sock, client in zip(socks, clients):
            mirror = TopicMirror()
            for frame in sock.frames:
                if frame["type"] in ("snapshot", "patch"):
                    assert mirror.apply(frame) != "resync"
            for topic in client.subs:
                if topic in hub.states:
                    assert mirror.payload(topic) == hub.latest(topic), topic
        await hub.close_all()

    run(scenario())


def test_resync_of_an_up_to_date_client_still_sends_a_fresh_snapshot():
    """Regression: a client already holding the latest version used to get nothing back."""
    async def scenario():
        hub, sock = Hub(), FakeSocket()
        client = sock.attach(hub)
        client.subscribe([TOPIC])
        hub.apply_update(upd(1, rows()))
        await settle()
        assert len(sock.frames) == 1
        client.request_resync(TOPIC)
        await settle()
        assert len(sock.frames) == 2 and sock.frames[-1]["type"] == "snapshot" and sock.frames[-1]["seq"] == 1
        await hub.close_all()

    run(scenario())
