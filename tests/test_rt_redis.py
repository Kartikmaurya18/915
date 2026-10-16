"""Leader election, fencing, fan-in and recovery against a REAL Redis.

Set TEST_REDIS_URL (e.g. redis://localhost:56379/0) to run these; they are skipped otherwise.
Every test works under its own random key prefix and only ever deletes keys under it, so a
shared Redis is safe (we never FLUSH anything).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import pytest
import redis

from src.realtime import metrics
from src.realtime.hub import Hub, TopicUpdate
from src.realtime.publisher import TopicPublisher
from src.realtime.redis_bus import (
    LeaderElector,
    RedisSink,
    RedisSubscriber,
    decode,
    encode,
)

URL = os.getenv("TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_REDIS_URL not set")

TOPIC = "options:NIFTY"


@pytest.fixture
def r():
    client = redis.Redis.from_url(URL, decode_responses=True)
    yield client
    client.close()


@pytest.fixture
def prefix(r):
    p = f"rt-test-{uuid.uuid4().hex[:8]}:"
    yield p
    for key in r.scan_iter(match=f"{p}*"):
        r.delete(key)


def elector(r, prefix, name, ttl_ms=6000):
    return LeaderElector(r, name, prefix, ttl_ms)


def update(seq, epoch, n=1):
    return TopicUpdate(TOPIC, epoch, seq, ts=1_000 + seq, pub=int(time.time() * 1000), payload={"n": n, "seq": seq})


def snapshot_key(prefix):
    return f"{prefix}snap:{TOPIC}"


def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# -- message codec -----------------------------------------------------------------


def test_codec_roundtrip():
    u = TopicUpdate("index:NIFTY", 1_776_000_000_123, 42, 1_000, 2_000, {"a": [1, None, {"b": "c"}]})
    assert decode(encode(u)) == u


# -- leader election ---------------------------------------------------------------


def test_only_one_node_can_lead(r, prefix):
    a, b = elector(r, prefix, "node-a"), elector(r, prefix, "node-b")
    fence_a = a.try_acquire()
    assert fence_a and a.is_leader
    assert b.try_acquire() is None and not b.is_leader
    assert r.get(f"{prefix}leader:holder") == "node-a"
    assert a.renew() and not b.renew()


def test_release_lets_the_standby_take_over_with_a_higher_fence(r, prefix):
    a, b = elector(r, prefix, "node-a"), elector(r, prefix, "node-b")
    fence_a = a.try_acquire()
    a.release()
    assert not a.is_leader and r.get(f"{prefix}leader:holder") is None
    fence_b = b.try_acquire()
    assert fence_b and fence_b > fence_a


def test_an_unrenewed_lease_expires_and_the_old_leader_knows_it(r, prefix):
    a, b = elector(r, prefix, "node-a", ttl_ms=300), elector(r, prefix, "node-b", ttl_ms=300)
    assert a.try_acquire()
    assert b.try_acquire() is None
    time.sleep(0.45)  # a "crashed": no renewal
    fence_b = b.try_acquire()
    assert fence_b and b.is_leader
    assert a.renew() is False and not a.is_leader  # a finds out the moment it tries to renew


def test_a_leader_that_cannot_confirm_its_lease_stops_believing_it_is_leader(r, prefix, monkeypatch):
    a = elector(r, prefix, "node-a", ttl_ms=400)
    assert a.try_acquire() and a.is_leader

    def unreachable(*_a, **_k):
        raise redis.ConnectionError("partitioned")

    monkeypatch.setattr(a, "_renew", unreachable)
    assert a.renew() is True  # error right after a confirmation: still inside the lease
    time.sleep(0.4)  # ... but past ~80% of the TTL without confirmation, another node may lead
    assert a.renew() is False and not a.is_leader


def test_failover_takes_about_one_ttl(r, prefix):
    ttl = 500
    a, b = elector(r, prefix, "node-a", ttl_ms=ttl), elector(r, prefix, "node-b", ttl_ms=ttl)
    a.try_acquire()
    crashed_at = time.monotonic()  # a never renews again
    while b.try_acquire() is None:
        time.sleep(0.02)
        assert time.monotonic() - crashed_at < 3
    took = time.monotonic() - crashed_at
    assert 0.3 < took < 1.5, took  # bounded by the lease TTL, not by anything else


def test_fence_never_goes_backwards_even_if_redis_loses_its_data(r, prefix):
    a, b = elector(r, prefix, "node-a"), elector(r, prefix, "node-b")
    first = a.try_acquire()
    a.release()
    for key in r.scan_iter(match=f"{prefix}*"):  # simulate total data loss for this namespace
        r.delete(key)
    second = b.try_acquire()
    assert second > first, "a regressed epoch would make every gateway ignore the new leader"


def test_fence_is_a_plausible_epoch_in_milliseconds(r, prefix):
    fence = elector(r, prefix, "node-a").try_acquire()
    assert abs(fence - time.time() * 1000) < 60_000  # time-based, so it is also useful when debugging


# -- fencing ---------------------------------------------------------------------------


def test_a_stale_leader_cannot_overwrite_the_new_leaders_data(r, prefix):
    old, new = elector(r, prefix, "old-leader", ttl_ms=300), elector(r, prefix, "new-leader", ttl_ms=5000)
    old.try_acquire()
    old_sink = RedisSink(r, old, prefix)
    old_sink([update(1, old.fence)])
    assert json.loads(r.get(snapshot_key(prefix)))["e"] == old.fence

    time.sleep(0.45)  # the old leader stalls past its lease (GC pause / partition) ...
    new.try_acquire()
    RedisSink(r, new, prefix)([update(1, new.fence, n=2)])
    held = json.loads(r.get(snapshot_key(prefix)))
    assert held["e"] == new.fence

    old_sink([update(99, old.fence, n=666)])  # ... and wakes up still believing it leads
    assert json.loads(r.get(snapshot_key(prefix))) == held, "stale leader overwrote newer data"
    assert old.lost, "the rejected publish must demote the stale leader immediately"
    assert metrics.publishes.labels("fenced")._value.get() >= 1


def test_a_node_that_reacquires_the_lease_cannot_publish_with_its_old_fence(r, prefix):
    a = elector(r, prefix, "node-a")
    old_fence = a.try_acquire()
    r.delete(f"{prefix}leader:holder")  # lease lost
    new_fence = a.try_acquire()
    assert new_fence != old_fence
    sink = RedisSink(r, a, prefix)
    a.fence = old_fence  # a bug/replay presenting the OLD token
    sink([update(1, old_fence)])
    assert r.get(snapshot_key(prefix)) is None and a.lost


def test_non_leaders_publish_nothing(r, prefix):
    a = elector(r, prefix, "node-a")  # never acquired
    RedisSink(r, a, prefix)([update(1, 1)])
    assert r.get(snapshot_key(prefix)) is None


# -- fan-in to the hub -----------------------------------------------------------------


def run_with_subscriber(prefix, scenario, **kwargs):
    async def main():
        hub = Hub()
        sub = RedisSubscriber(hub, URL, prefix, client_name=f"rt-test-{uuid.uuid4().hex[:6]}", **kwargs)
        sub.start()
        try:
            await scenario(hub, sub)
        finally:
            await sub.stop()

    asyncio.run(main())


async def eventually(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def test_published_updates_reach_the_hub_in_order(r, prefix):
    leader = elector(r, prefix, "node-a")
    leader.try_acquire()
    sink = RedisSink(r, leader, prefix)

    async def scenario(hub, sub):
        assert await eventually(lambda: sub.connected)
        for seq in range(1, 6):
            sink([update(seq, leader.fence, n=seq)])
        assert await eventually(lambda: hub.states.get(TOPIC) and hub.states[TOPIC].seq == 5)
        assert hub.states[TOPIC].epoch == leader.fence and hub.latest(TOPIC) == {"n": 5, "seq": 5}

    run_with_subscriber(prefix, scenario)


def test_a_gateway_that_starts_late_catches_up_from_snapshots(r, prefix):
    leader = elector(r, prefix, "node-a")
    leader.try_acquire()
    sink = RedisSink(r, leader, prefix)
    sink([update(1, leader.fence), update(2, leader.fence, n=2)])
    sink([TopicUpdate("index:NIFTY", leader.fence, 7, 1, 1, {"price": 24850.5})])

    async def scenario(hub, sub):
        assert await eventually(lambda: len(hub.states) == 2)
        assert hub.latest("index:NIFTY") == {"price": 24850.5} and hub.states[TOPIC].seq == 2

    run_with_subscriber(prefix, scenario)


def test_ordering_survives_a_failover(r, prefix):
    a, b = elector(r, prefix, "node-a", ttl_ms=300), elector(r, prefix, "node-b", ttl_ms=5000)
    a.try_acquire()
    sink_a = RedisSink(r, a, prefix)

    async def scenario(hub, sub):
        assert await eventually(lambda: sub.connected)
        sink_a([update(i, a.fence, n=i) for i in (1, 2, 3)])
        assert await eventually(lambda: hub.states.get(TOPIC) and hub.states[TOPIC].seq == 3)

        await asyncio.sleep(0.45)  # a stops renewing; b takes over and restarts seq at 1
        assert b.try_acquire()
        RedisSink(r, b, prefix)([update(1, b.fence, n=100)])
        assert await eventually(lambda: hub.states[TOPIC].epoch == b.fence)

        sink_a([update(4, a.fence, n=666)])  # the old leader tries once more: fenced out at Redis
        await asyncio.sleep(0.2)
        assert hub.states[TOPIC].epoch == b.fence and hub.latest(TOPIC)["n"] == 100

    run_with_subscriber(prefix, scenario)


def test_the_hub_ignores_duplicates_and_stale_messages_delivered_over_pubsub(r, prefix):
    async def scenario(hub, sub):
        assert await eventually(lambda: sub.connected)
        for seq in (1, 2, 2, 1, 3):  # duplicated and reordered delivery
            r.publish(f"{prefix}ch:{TOPIC}", encode(update(seq, 5, n=seq)))
        assert await eventually(lambda: hub.states.get(TOPIC) and hub.states[TOPIC].seq == 3)
        assert hub.latest(TOPIC)["n"] == 3
        r.publish(f"{prefix}ch:{TOPIC}", "{not json")  # garbage must not kill the subscriber
        r.publish(f"{prefix}ch:{TOPIC}", encode(update(4, 5, n=4)))
        assert await eventually(lambda: hub.states[TOPIC].seq == 4)

    run_with_subscriber(prefix, scenario)


def kill_my_connections(r, name):
    killed = 0
    for conn in r.client_list():
        if conn.get("name") == name:
            r.client_kill_filter(_id=conn["id"])
            killed += 1
    return killed


def test_the_subscriber_reconnects_and_recovers_what_it_missed(r, prefix):
    leader = elector(r, prefix, "node-a")
    leader.try_acquire()
    sink = RedisSink(r, leader, prefix)

    async def main():
        hub = Hub()
        name = f"rt-test-{uuid.uuid4().hex[:6]}"
        sub = RedisSubscriber(hub, URL, prefix, client_name=name)
        sub.start()
        try:
            assert await eventually(lambda: sub.connected)
            sink([update(1, leader.fence)])
            assert await eventually(lambda: hub.states.get(TOPIC) is not None)
            reconnects_before = metrics.redis_reconnects._value.get()

            assert kill_my_connections(r, name) > 0  # the network drops under the gateway
            assert await eventually(lambda: not sub.connected, 3)
            sink([update(2, leader.fence, n=2), update(3, leader.fence, n=3)])  # published while it is down

            assert await eventually(lambda: sub.connected, 8), "did not reconnect"
            assert await eventually(lambda: hub.states[TOPIC].seq == 3, 5), "missed updates were not recovered"
            assert metrics.redis_reconnects._value.get() > reconnects_before

            sink([update(4, leader.fence, n=4)])  # and live updates flow again
            assert await eventually(lambda: hub.states[TOPIC].seq == 4)
        finally:
            await sub.stop()

    asyncio.run(main())


def test_periodic_reconcile_heals_a_lost_pubsub_message(r, prefix):
    leader = elector(r, prefix, "node-a")
    leader.try_acquire()
    sink = RedisSink(r, leader, prefix)

    async def scenario(hub, sub):
        assert await eventually(lambda: sub.connected)
        sink([update(1, leader.fence)])
        assert await eventually(lambda: hub.states.get(TOPIC) is not None)
        # write the snapshot WITHOUT publishing: exactly what a dropped pub/sub message looks like
        r.set(snapshot_key(prefix), encode(update(2, leader.fence, n=2)))
        assert await eventually(lambda: hub.states[TOPIC].seq == 2, 4)

    run_with_subscriber(prefix, scenario, reconcile_seconds=0.3)


def test_gateways_keep_serving_stale_state_when_redis_loses_everything(r, prefix):
    """Total Redis data loss must not blank the gateways, and the next leader must win."""
    a = elector(r, prefix, "node-a")
    a.try_acquire()

    async def scenario(hub, sub):
        assert await eventually(lambda: sub.connected)
        RedisSink(r, a, prefix)([update(1, a.fence, n=1)])
        assert await eventually(lambda: hub.states.get(TOPIC) is not None)
        old_epoch = hub.states[TOPIC].epoch

        for key in r.scan_iter(match=f"{prefix}*"):  # Redis restarts empty
            r.delete(key)
        await asyncio.sleep(0.2)
        assert hub.latest(TOPIC) == {"n": 1, "seq": 1}  # last known data is still served (feed_age grows)
        assert hub.feed_age_ms() is not None

        b = elector(r, prefix, "node-b")
        assert b.try_acquire() and b.fence > old_epoch
        RedisSink(r, b, prefix)([update(1, b.fence, n=2)])
        assert await eventually(lambda: hub.latest(TOPIC) == {"n": 2, "seq": 1}), "new leader's data was rejected"

    run_with_subscriber(prefix, scenario)


# -- the publisher feeding Redis end to end --------------------------------------------


def test_topic_publisher_to_redis_to_hub(r, prefix):
    leader = elector(r, prefix, "node-a")
    leader.try_acquire()
    publisher = TopicPublisher(RedisSink(r, leader, prefix), "NIFTY", epoch=leader.fence)
    snap = {
        "underlying": "NIFTY", "price": 24800.0, "timestamp": "2026-10-16T10:00:00+05:30",
        "option_chain": {"strikes": [{"strike": 24800.0, "call_ltp": 100.0}]}, "candles": {}, "paper_trading": {"secret": 1},
    }

    async def scenario(hub, sub):
        assert await eventually(lambda: sub.connected)
        publisher.on_snapshot(snap)
        assert await eventually(lambda: "options:NIFTY" in hub.states and "index:NIFTY" in hub.states)
        assert "paper_trading" not in json.dumps([s.payload for s in hub.states.values()])
        snap["price"] = 24801.0
        publisher.on_snapshot(snap)
        assert await eventually(lambda: hub.states["index:NIFTY"].seq == 2)
        assert hub.states["options:NIFTY"].seq == 1  # unchanged topic: nothing was re-published

    run_with_subscriber(prefix, scenario)
