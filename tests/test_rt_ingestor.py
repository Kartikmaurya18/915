"""The ingestor and the control channel, against a real Redis.

Skipped unless TEST_REDIS_URL is set. Each test uses its own key prefix.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
import redis

from src.config import load_config
from src.ingestor import Ingestor
from src.realtime.control import ALLOWED, ControlClient, ControlRemoteError, ControlUnavailable
from src.realtime.hub import Hub
from src.realtime.redis_bus import RedisSubscriber

URL = os.getenv("TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_REDIS_URL not set")
ROOT = Path(__file__).resolve().parent.parent
INDEX = "index:NIFTY"
TTL_MS = 1200


@pytest.fixture
def r():
    client = redis.Redis.from_url(URL, decode_responses=True)
    yield client
    client.close()


@pytest.fixture
def prefix(r):
    p = f"rt-ing-{uuid.uuid4().hex[:8]}:"
    yield p
    for key in r.scan_iter(match=f"{p}*"):
        r.delete(key)


def fast_cfg() -> dict:
    cfg = load_config()
    cfg["poll_interval_seconds"] = 0.15
    cfg["persistence"] = {"enabled": False}
    return cfg


class Node:
    """An in-process ingestor running on its own thread."""

    def __init__(self, prefix: str, name: str):
        self.ingestor = Ingestor(URL, prefix, node_id=name, ttl_ms=TTL_MS, cfg=fast_cfg())
        self.thread = threading.Thread(target=self.ingestor.run_forever, name=name, daemon=True)

    def start(self) -> "Node":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.ingestor.request_stop()
        self.thread.join(timeout=10)

    @property
    def leading(self) -> bool:
        return self.ingestor.became_leader.is_set()


async def eventually(predicate, timeout=8.0, interval=0.03):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


def run_hub(prefix, scenario):
    async def main():
        hub = Hub()
        sub = RedisSubscriber(hub, URL, prefix, client_name=f"rt-test-{uuid.uuid4().hex[:6]}")
        sub.start()
        try:
            await scenario(hub, sub)
        finally:
            await sub.stop()

    asyncio.run(main())


# -- leadership ------------------------------------------------------------------------


def test_exactly_one_ingestor_runs_the_engine_and_publishes(prefix):
    a, b = Node(prefix, "node-a").start(), Node(prefix, "node-b").start()

    async def scenario(hub, sub):
        try:
            assert await eventually(lambda: a.leading or b.leading), "nobody became leader"
            await asyncio.sleep(0.5)
            leaders = [n for n in (a, b) if n.leading]
            standby = [n for n in (a, b) if not n.leading]
            assert len(leaders) == 1 and len(standby) == 1  # never two
            assert leaders[0].ingestor.engine.is_running  # one active provider connection ...
            assert not standby[0].ingestor.engine.is_running  # ... and the standby holds none
            assert await eventually(lambda: INDEX in hub.states), "leader published nothing"
            assert hub.states[INDEX].epoch == leaders[0].ingestor.elector.fence
        finally:
            a.stop()
            b.stop()

    run_hub(prefix, scenario)


def test_graceful_handover_is_fast_and_the_epoch_advances(prefix):
    a, b = Node(prefix, "node-a").start(), Node(prefix, "node-b").start()

    async def scenario(hub, sub):
        try:
            assert await eventually(lambda: a.leading or b.leading and INDEX in hub.states)
            assert await eventually(lambda: INDEX in hub.states)
            first, second = (a, b) if a.leading else (b, a)
            old_epoch = hub.states[INDEX].epoch

            started = time.time()
            first.stop()  # SIGTERM-equivalent: releases the lease
            assert await eventually(lambda: second.leading, 6)
            assert await eventually(lambda: hub.states[INDEX].epoch > old_epoch, 6)
            took = time.time() - started
            assert took < (TTL_MS / 1000.0), f"graceful handover should not wait out the lease ({took:.2f}s)"
            assert second.ingestor.engine.is_running and not first.ingestor.engine.is_running
        finally:
            a.stop()
            b.stop()

    run_hub(prefix, scenario)


def test_a_stale_leader_that_wakes_up_is_fenced_and_stands_down(prefix, r):
    """The nightmare case: the leader stalls past its lease, another node takes over, then the
    old leader resumes still believing it leads. Redis must reject it, and it must stop."""
    a = Node(prefix, "node-a").start()

    async def scenario(hub, sub):
        b = None
        try:
            assert await eventually(lambda: a.leading and INDEX in hub.states)
            a_epoch = hub.states[INDEX].epoch

            # Freeze A (as a long GC pause / VM stall would) and let its lease lapse.
            a.ingestor.elector.renew = lambda: True  # A believes everything is fine
            r.delete(f"{prefix}leader:holder")  # the lease expires in Redis
            b = Node(prefix, "node-b").start()
            assert await eventually(lambda: b.leading, 8)
            assert await eventually(lambda: hub.states[INDEX].epoch > a_epoch, 5)
            b_epoch = hub.states[INDEX].epoch

            # A is still ticking and still thinks it leads: its next publish must be rejected ...
            assert await eventually(lambda: not a.leading, 8), "the stale leader never stood down"
            assert not a.ingestor.engine.is_running  # ... and it released the provider connection
            # ... and nothing it sent overwrote the new leader's data.
            await asyncio.sleep(0.6)
            assert hub.states[INDEX].epoch == b_epoch
        finally:
            a.stop()
            if b:
                b.stop()

    run_hub(prefix, scenario)


def test_the_exit_worker_only_runs_on_the_leader(prefix, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    a, b = Node(prefix, "node-a"), Node(prefix, "node-b")
    assert a.ingestor.exit_worker is not None
    a.start()

    async def scenario(hub, sub):
        try:
            assert await eventually(lambda: a.leading)
            assert a.ingestor.exit_worker in a.ingestor.engine._chain_listeners
            b.start()
            await asyncio.sleep(0.5)
            assert b.ingestor.exit_worker not in b.ingestor.engine._chain_listeners  # standby decides no exits
            a.stop()
            assert a.ingestor.exit_worker not in a.ingestor.engine._chain_listeners  # and the old leader stops
            assert await eventually(lambda: b.leading, 6)
            assert b.ingestor.exit_worker in b.ingestor.engine._chain_listeners
        finally:
            a.stop()
            b.stop()

    run_hub(prefix, scenario)


# -- a real crash ------------------------------------------------------------------------


def spawn(prefix, name):
    env = {
        **os.environ,
        "REDIS_URL": URL,
        "RT_PREFIX": prefix,
        "INGESTOR_LEASE_TTL_MS": str(TTL_MS),
        "INGESTOR_METRICS_PORT": "0",
        "POLL_INTERVAL_SECONDS": "0.15",
        "PERSISTENCE_ENABLED": "false",
        "AUTH_MODE": "disabled",
        "APP_ENV": "development",
        "MPLBACKEND": "Agg",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "src.ingestor", "--node-id", name, "--metrics-port", "0"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def test_killing_the_leader_process_fails_over_within_about_one_lease(prefix, r):
    procs = {"proc-a": spawn(prefix, "proc-a"), "proc-b": spawn(prefix, "proc-b")}

    async def scenario(hub, sub):
        try:
            assert await eventually(lambda: INDEX in hub.states, 30), "no ingestor ever published (did they start?)"
            leader = r.get(f"{prefix}leader:holder")
            assert leader in procs
            old_epoch = hub.states[INDEX].epoch

            procs[leader].kill()  # a hard crash: no chance to release the lease
            killed_at = time.time()
            assert await eventually(lambda: hub.states[INDEX].epoch > old_epoch, 20), "no failover"
            gap = time.time() - killed_at

            survivor = "proc-b" if leader == "proc-a" else "proc-a"
            assert r.get(f"{prefix}leader:holder") == survivor
            # bounded by the lease TTL plus the survivor's poll interval and engine start-up
            assert gap < TTL_MS / 1000.0 * 4 + 3, f"failover took {gap:.1f}s"
            seq_before = hub.states[INDEX].seq
            assert await eventually(lambda: hub.states[INDEX].seq > seq_before, 5), "feed did not resume"
            print(f"\nfailover gap after hard kill (lease {TTL_MS} ms): {gap:.2f}s")
        finally:
            for proc in procs.values():
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)

    run_hub(prefix, scenario)


# -- control channel ----------------------------------------------------------------------


def test_control_rpc_is_answered_only_by_the_leader(prefix):
    node = Node(prefix, "node-a").start()

    async def main():
        client = ControlClient(URL, prefix, timeout=4)
        try:
            assert await eventually(lambda: node.leading)
            settings = await client.call("get_settings")
            assert settings["atm_method"] in ("delta", "price")
            logs = await client.call("get_recent_logs", 5, None)
            assert isinstance(logs, list) and len(logs) <= 5
            assert await client.call("get_strike_straddle_candles", 1.0) is None  # None round-trips as null
            with pytest.raises(ControlRemoteError) as bad:
                await client.call("set_atm_config", "not-a-method", None)
            assert bad.value.kind == "ValueError"
        finally:
            node.stop()
            await client.close()

    asyncio.run(main())


def test_control_rpc_fails_cleanly_without_a_leader(prefix):
    async def main():
        client = ControlClient(URL, prefix, timeout=0.6)
        try:
            with pytest.raises(ControlUnavailable):
                await client.call("get_settings")
        finally:
            await client.close()

    asyncio.run(main())


def test_only_whitelisted_methods_can_be_called(prefix, r):
    node = Node(prefix, "node-a").start()

    async def main():
        client = ControlClient(URL, prefix, timeout=3)
        try:
            assert await eventually(lambda: node.leading)
            for method in ("close", "start", "__class__", "_build_snapshot", "open_paper_position", "exec"):
                with pytest.raises(ValueError):
                    await client.call(method)  # refused client-side

            # ... and a forged request that bypasses the client is refused server-side too
            import json

            forged_id = uuid.uuid4().hex
            r.rpush(f"{prefix}ctl:req", json.dumps({"id": forged_id, "m": "close", "a": [], "k": {}, "t": time.time()}))
            reply = r.blpop([f"{prefix}ctl:resp:{forged_id}"], timeout=3)
            assert reply is not None and "not allowed" in json.loads(reply[1])["error"]["message"]
            assert node.ingestor.engine.is_running  # the engine was not closed
        finally:
            node.stop()
            await client.close()

    asyncio.run(main())
    assert {"close", "start", "stop", "open_paper_position"}.isdisjoint(ALLOWED)


def test_stale_control_requests_are_ignored(prefix, r):
    node = Node(prefix, "node-a").start()

    async def main():
        try:
            assert await eventually(lambda: node.leading)
            import json

            request_id = uuid.uuid4().hex
            r.rpush(f"{prefix}ctl:req", json.dumps({"id": request_id, "m": "get_settings", "a": [], "k": {}, "t": time.time() - 120}))
            assert r.blpop([f"{prefix}ctl:resp:{request_id}"], timeout=2) is None  # the caller gave up long ago
        finally:
            node.stop()

    asyncio.run(main())
