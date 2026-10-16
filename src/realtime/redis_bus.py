"""Redis transport: leader lease + fencing (ingestor side), fan-in subscriber (gateway side).

Keys (all under a configurable prefix, default "rt:"):
    rt:leader:holder    node id of the current leader; expires unless renewed
    rt:leader:counter   fencing counter: max(previous + 1, current ms) on every takeover, so it
                        never goes backwards even if Redis loses its data
    rt:leader:fence     the fencing token of the CURRENT leader
    rt:snap:<topic>     latest message of each topic (what a new/recovering gateway reads)
    rt:ch:<topic>       pub/sub channel carrying every message

Safety: a publish is accepted only if the caller is still the holder AND presents the
current fencing token, checked atomically inside Redis (Lua). A leader that stalled past
its lease (GC pause, network partition) and wakes up after someone else took over is
therefore rejected instead of overwriting newer data. The fencing token doubles as the
message `epoch`, so subscribers also order messages across failovers.

Pub/sub is lossy by design. Every message carries the FULL topic payload and a version,
so a lost message is healed by the next one, and a gateway that (re)connects reads
`rt:snap:*` to catch up. Nothing here is a durable log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Callable

import redis
import redis.asyncio as aioredis

from src.realtime import metrics
from src.realtime.hub import Hub, TopicUpdate

log = logging.getLogger("market_data.realtime")

DEFAULT_PREFIX = "rt:"


def default_prefix() -> str:
    """Key prefix, read when an object is built so tests and co-hosted environments can isolate
    themselves with RT_PREFIX."""
    return os.getenv("RT_PREFIX", DEFAULT_PREFIX)

_ACQUIRE = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then
  -- The fencing token is also the message epoch, so it must NEVER go backwards, even if
  -- Redis loses its data and the counter restarts at 1: take max(counter, current ms).
  local t = redis.call('TIME')
  local now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
  local fence = redis.call('INCR', KEYS[2])
  if fence < now_ms then
    fence = now_ms
    redis.call('SET', KEYS[2], fence)
  end
  redis.call('SET', KEYS[3], fence)
  return fence
end
return 0
"""

_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_PUBLISH = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
redis.call('SET', KEYS[3], ARGV[3], 'EX', ARGV[4])
redis.call('PUBLISH', KEYS[4], ARGV[3])
return 1
"""


def redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://localhost:6379/0")


def user_channel_prefix(prefix: str | None = None) -> str:
    return f"{prefix if prefix is not None else default_prefix()}user:"


def publish_user_event(client: redis.Redis, user_id: str, prefix: str | None = None) -> None:
    """Announce that one user's paper book changed. Every gateway receives it and tells that user's
    open sockets. Carries no data (the browser refetches over authenticated REST), so nothing private
    travels through Redis pub/sub and a lost message is harmless: the next poll heals it."""
    client.publish(user_channel_prefix(prefix) + user_id, "paper")


def encode(update: TopicUpdate) -> str:
    return json.dumps(
        {"t": update.topic, "e": update.epoch, "s": update.seq, "ts": update.ts, "pub": update.pub, "d": update.payload},
        separators=(",", ":"),
    )


def decode(raw: str) -> TopicUpdate:
    msg = json.loads(raw)
    return TopicUpdate(msg["t"], int(msg["e"]), int(msg["s"]), int(msg["ts"]), int(msg["pub"]), msg["d"])


# -- ingestor side -------------------------------------------------------------------


class LeaderElector:
    """Lease-based leadership with fencing tokens. Synchronous; owned by one thread."""

    def __init__(self, client: redis.Redis, node_id: str, prefix: str | None = None, ttl_ms: int = 6000):
        prefix = prefix if prefix is not None else default_prefix()
        self.client = client
        self.node_id = node_id
        self.prefix = prefix
        self.ttl_ms = ttl_ms
        self.fence: int | None = None
        self.lost = False  # set by the publish path when Redis rejects our fencing token
        self._last_confirmed = 0.0
        self._acquire = client.register_script(_ACQUIRE)
        self._renew = client.register_script(_RENEW)
        self._release = client.register_script(_RELEASE)

    @property
    def keys(self) -> tuple[str, str, str]:
        p = self.prefix
        return f"{p}leader:holder", f"{p}leader:counter", f"{p}leader:fence"

    @property
    def is_leader(self) -> bool:
        if self.fence is None or self.lost:
            return False
        # If we could not confirm the lease for most of a TTL, someone else may hold it now.
        return (time.monotonic() - self._last_confirmed) < (self.ttl_ms / 1000.0) * 0.8

    def try_acquire(self) -> int | None:
        """The new fencing token if we became leader, else None."""
        try:
            fence = int(self._acquire(keys=list(self.keys), args=[self.node_id, self.ttl_ms]))
        except redis.RedisError:
            return None
        if not fence:
            return None
        self.fence, self.lost, self._last_confirmed = fence, False, time.monotonic()
        metrics.is_leader.set(1)
        metrics.leader_changes.labels("acquired").inc()
        return fence

    def renew(self) -> bool:
        if self.fence is None or self.lost:
            return False
        try:
            ok = bool(self._renew(keys=[self.keys[0]], args=[self.node_id, self.ttl_ms]))
        except redis.RedisError:
            return self.is_leader  # cannot confirm; stay leader only until the lease could have lapsed
        if ok:
            self._last_confirmed = time.monotonic()
            return True
        self.step_down("lease_lost")
        return False

    def step_down(self, reason: str = "released") -> None:
        if self.fence is not None:
            metrics.is_leader.set(0)
            metrics.leader_changes.labels(reason).inc()
        self.fence = None

    def release(self) -> None:
        """Give the lease back now so a standby need not wait out the TTL. Safe to call at
        any time: the script only deletes the key if THIS node still holds it."""
        try:
            self._release(keys=[self.keys[0]], args=[self.node_id])
        except redis.RedisError:
            pass
        self.step_down("released")

    def current_leader(self) -> str | None:
        try:
            return self.client.get(self.keys[0])
        except redis.RedisError:
            return None


class RedisSink:
    """TopicPublisher sink: publishes each update through the fenced Lua script."""

    def __init__(
        self,
        client: redis.Redis,
        elector: LeaderElector,
        prefix: str | None = None,
        snapshot_ttl_seconds: int | None = None,
    ):
        prefix = prefix if prefix is not None else default_prefix()
        self.client = client
        self.elector = elector
        self.prefix = prefix
        self.snapshot_ttl = snapshot_ttl_seconds or int(os.getenv("RT_SNAPSHOT_TTL_SECONDS", "3600"))
        self._script = client.register_script(_PUBLISH)

    def __call__(self, updates: list[TopicUpdate]) -> None:
        fence = self.elector.fence
        if fence is None or self.elector.lost:
            metrics.publishes.labels("skipped").inc(len(updates))
            return
        holder_key, _counter, fence_key = self.elector.keys
        started = time.perf_counter()
        try:
            pipe = self.client.pipeline(transaction=False)
            for update in updates:
                self._script(
                    keys=[holder_key, fence_key, f"{self.prefix}snap:{update.topic}", f"{self.prefix}ch:{update.topic}"],
                    args=[self.elector.node_id, fence, encode(update), self.snapshot_ttl],
                    client=pipe,
                )
            results = pipe.execute()
        except redis.RedisError as exc:
            metrics.publishes.labels("error").inc(len(updates))
            log.warning("realtime publish failed: %s", exc)
            return
        metrics.publish_seconds.observe(time.perf_counter() - started)
        for result in results:
            if int(result) == 1:
                metrics.publishes.labels("ok").inc()
            else:
                metrics.publishes.labels("fenced").inc()
                self.elector.lost = True  # we are no longer the leader: stop at once


# -- gateway side --------------------------------------------------------------------


class RedisSubscriber:
    """Feeds a Hub from Redis and keeps doing so across Redis outages."""

    def __init__(
        self,
        hub: Hub,
        url: str | None = None,
        prefix: str | None = None,
        reconcile_seconds: float | None = None,
        on_state: Callable[[bool], None] | None = None,
        client_name: str = "rt-gateway",
    ):
        self.client_name = client_name
        self.hub = hub
        self.url = url or redis_url()
        self.prefix = prefix if prefix is not None else default_prefix()
        self.reconcile_seconds = reconcile_seconds if reconcile_seconds is not None else float(os.getenv("RT_RECONCILE_SECONDS", "30"))
        self.connected = False
        self._on_state = on_state
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def _set_connected(self, value: bool) -> None:
        if value != self.connected:
            self.connected = value
            metrics.redis_up.set(1 if value else 0)
            if self._on_state:
                self._on_state(value)

    def _apply(self, raw: str) -> None:
        try:
            self.hub.apply_update(decode(raw))
        except (ValueError, KeyError, TypeError):
            metrics.updates.labels("malformed").inc()

    async def _load_snapshots(self, client: aioredis.Redis) -> None:
        keys = [key async for key in client.scan_iter(match=f"{self.prefix}snap:*", count=200)]
        if keys:
            for raw in await client.mget(keys):
                if raw:
                    self._apply(raw)

    async def _run(self) -> None:
        backoff = 0.25
        user_prefix = user_channel_prefix(self.prefix)
        while not self._stop.is_set():
            client = pubsub = None
            try:
                client = aioredis.from_url(
                    self.url, decode_responses=True, health_check_interval=10, socket_keepalive=True, client_name=self.client_name
                )
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                # subscribe FIRST, so reading snapshots cannot leave a gap
                await pubsub.psubscribe(f"{self.prefix}ch:*", f"{self.prefix}user:*")
                await self._load_snapshots(client)
                self._set_connected(True)
                backoff, last_reconcile = 0.25, time.monotonic()
                while not self._stop.is_set():
                    message = await pubsub.get_message(timeout=1.0)
                    if message and message.get("type") == "pmessage":
                        channel = message.get("channel") or ""
                        if channel.startswith(user_prefix):
                            self.hub.notify_user(channel[len(user_prefix):])
                        else:
                            self._apply(message["data"])
                    if time.monotonic() - last_reconcile >= self.reconcile_seconds:
                        await self._load_snapshots(client)  # heals anything pub/sub dropped
                        last_reconcile = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # connection loss, Redis restart, timeout ...
                self._set_connected(False)
                metrics.redis_reconnects.inc()
                log.warning("redis subscriber disconnected (%s); retrying in %.1fs", type(exc).__name__, backoff)
                await asyncio.sleep(backoff + random.uniform(0, backoff / 2))
                backoff = min(backoff * 2, 10.0)
            finally:
                for closer in (pubsub, client):
                    if closer is not None:
                        try:
                            await closer.aclose()
                        except Exception:
                            pass
        self._set_connected(False)
