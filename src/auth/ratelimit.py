"""Sliding-window rate limiting: in-process, or shared across replicas through Redis.

A limit that lives in one process is multiplied by the number of replicas (three gateways would
allow three times the login attempts), so production shares the counters in Redis. Redis is not
allowed to become a way to lock everyone out, though: if it is unreachable the limiter degrades to
the in-process counters for the affected calls and recovers by itself when Redis returns.

Pick the backend with RATE_LIMIT_BACKEND = "memory" | "redis" | "auto" (default). "auto" uses Redis
when REDIS_URL is set. All limiters expose the same small interface, so callers do not care which.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque
from typing import Callable

import redis
from prometheus_client import Counter

log = logging.getLogger("market_data.ratelimit")

backend_errors = Counter("rate_limit_backend_errors_total", "Rate-limit calls that fell back to in-process counters", ["limiter"])


class SlidingWindowLimiter:
    """In-process limiter (also the fallback for the Redis one)."""

    def __init__(
        self,
        max_events: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 50_000,
    ):
        self.max_events = max_events
        self.window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        events = self._events.get(key)
        if events is None:
            events = self._events[key] = deque()
        cutoff = now - self.window
        while events and events[0] <= cutoff:
            events.popleft()
        return events

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, self._clock())) >= self.max_events

    def record(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            self._prune(key, now).append(now)
            if len(self._events) > self._max_keys:  # bound memory under key-spraying
                for stale in [k for k, v in self._events.items() if not v][: self._max_keys // 10]:
                    self._events.pop(stale, None)

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)

    def hit(self, key: str) -> bool:
        """Count one event; return False (without counting) if already over the limit."""
        with self._lock:
            now = self._clock()
            events = self._prune(key, now)
            if len(events) >= self.max_events:
                return False
            events.append(now)
            return True

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


# The server's own clock (TIME) is used for every comparison, so replicas with skewed clocks agree.
_PRUNE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - tonumber(ARGV[1]))
"""
_HIT = _PRUNE + """
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZADD', KEYS[1], now, now .. ':' .. ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[1])
return 1
"""
_RECORD = _PRUNE + """
redis.call('ZADD', KEYS[1], now, now .. ':' .. ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[1])
return redis.call('ZCARD', KEYS[1])
"""
_BLOCKED = _PRUNE + """
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 1 end
return 0
"""


class RedisSlidingWindowLimiter:
    """The same sliding window, stored in a Redis sorted set per key. Raises redis.RedisError on trouble;
    wrap it in `ResilientLimiter` to get the fallback behaviour."""

    def __init__(self, client: redis.Redis, name: str, max_events: int, window_seconds: float, prefix: str = "rt:"):
        self.client = client
        self.name = name
        self.max_events = max_events
        self.window = window_seconds
        self.prefix = f"{prefix}rl:{name}:"
        self._hit = client.register_script(_HIT)
        self._record = client.register_script(_RECORD)
        self._blocked = client.register_script(_BLOCKED)

    def _key(self, key: str) -> str:
        return self.prefix + key

    def _args(self) -> list:
        return [int(self.window * 1000), self.max_events, uuid.uuid4().hex[:12]]

    def hit(self, key: str) -> bool:
        return bool(int(self._hit(keys=[self._key(key)], args=self._args())))

    def record(self, key: str) -> None:
        self._record(keys=[self._key(key)], args=self._args())

    def is_blocked(self, key: str) -> bool:
        return bool(int(self._blocked(keys=[self._key(key)], args=self._args())))

    def reset(self, key: str) -> None:
        self.client.delete(self._key(key))

    def clear(self) -> None:
        keys = list(self.client.scan_iter(match=self.prefix + "*", count=200))
        if keys:
            self.client.delete(*keys)


class ResilientLimiter:
    """Redis first; the in-process limiter whenever Redis cannot answer.

    After a failure Redis is not tried again for `retry_after` seconds, so an outage costs one slow
    call, not one per request. A shared limit is the goal; a degraded per-replica limit is far better
    than either blocking all logins or removing the limit altogether."""

    def __init__(self, primary: RedisSlidingWindowLimiter, fallback: SlidingWindowLimiter, retry_after: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self.primary = primary
        self.fallback = fallback
        self.max_events = fallback.max_events
        self.window = fallback.window
        self._retry_after = retry_after
        self._clock = clock
        self._down_until = 0.0
        self._last_log = 0.0

    @property
    def degraded(self) -> bool:
        return self._clock() < self._down_until

    def _call(self, method: str, key: str):
        if not self.degraded:
            try:
                return getattr(self.primary, method)(key)
            except (redis.RedisError, OSError) as exc:
                now = self._clock()
                self._down_until = now + self._retry_after
                backend_errors.labels(self.primary.name).inc()
                if now - self._last_log > 30:
                    self._last_log = now
                    log.warning("rate limiter %s: Redis unavailable (%s); using in-process counters", self.primary.name, type(exc).__name__)
        return getattr(self.fallback, method)(key)

    def hit(self, key: str) -> bool:
        return self._call("hit", key)

    def record(self, key: str) -> None:
        self._call("record", key)

    def is_blocked(self, key: str) -> bool:
        return self._call("is_blocked", key)

    def reset(self, key: str) -> None:
        self.fallback.reset(key)  # clear both: counts may exist on either side
        if not self.degraded:
            try:
                self.primary.reset(key)
            except (redis.RedisError, OSError):
                self._down_until = self._clock() + self._retry_after

    def clear(self) -> None:
        self.fallback.clear()
        try:
            self.primary.clear()
        except (redis.RedisError, OSError):
            pass


def rate_limit_backend() -> str:
    mode = os.getenv("RATE_LIMIT_BACKEND", "auto").strip().lower()
    if mode not in ("auto", "memory", "redis"):
        raise ValueError(f"RATE_LIMIT_BACKEND must be auto, memory or redis, got {mode!r}")
    if mode == "auto":
        return "redis" if os.getenv("REDIS_URL") else "memory"
    return mode


_client: redis.Redis | None = None
_client_lock = threading.Lock()


def _redis_client() -> redis.Redis:
    """One shared client with short timeouts: a slow Redis must not stall login requests."""
    global _client
    with _client_lock:
        if _client is None:
            _client = redis.Redis.from_url(
                os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                decode_responses=True,
                socket_timeout=0.5,
                socket_connect_timeout=0.5,
                client_name="rt-ratelimit",
            )
        return _client


def make_limiter(name: str, max_events: int, window_seconds: float):
    """The limiter for `name` using the configured backend."""
    local = SlidingWindowLimiter(max_events, window_seconds)
    if rate_limit_backend() == "memory":
        return local
    from src.realtime.redis_bus import default_prefix

    return ResilientLimiter(RedisSlidingWindowLimiter(_redis_client(), name, max_events, window_seconds, default_prefix()), local)
