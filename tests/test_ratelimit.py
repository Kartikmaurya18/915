"""Rate limiting: shared across replicas through Redis, and never a way to lock everyone out."""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest
import redis

from src.auth import ratelimit
from src.auth.ratelimit import RedisSlidingWindowLimiter, ResilientLimiter, SlidingWindowLimiter, backend_errors, make_limiter

REDIS_URL = os.getenv("TEST_REDIS_URL")
needs_redis = pytest.mark.skipif(not REDIS_URL, reason="TEST_REDIS_URL not set (these need a real Redis)")


# -- Redis-backed limiter ------------------------------------------------------------------


@pytest.fixture
def client():
    c = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    yield c
    c.close()


@pytest.fixture
def prefix():
    return f"test-{uuid.uuid4().hex[:8]}:"  # isolates each test inside a shared Redis


def limiter(client, prefix, name="login", max_events=3, window=300.0):
    return RedisSlidingWindowLimiter(client, name, max_events, window, prefix)


@needs_redis
def test_hit_allows_up_to_the_limit_then_refuses_without_counting(client, prefix):
    lim = limiter(client, prefix)
    assert [lim.hit("ip") for _ in range(5)] == [True, True, True, False, False]
    assert client.zcard(f"{prefix}rl:login:ip") == 3  # refused attempts are not recorded


@needs_redis
def test_keys_are_independent(client, prefix):
    lim = limiter(client, prefix, max_events=1)
    assert lim.hit("a") and lim.hit("b")
    assert not lim.hit("a")


@needs_redis
def test_record_and_is_blocked(client, prefix):
    lim = limiter(client, prefix, max_events=2)
    assert not lim.is_blocked("acct")
    lim.record("acct")
    assert not lim.is_blocked("acct")
    lim.record("acct")
    assert lim.is_blocked("acct")
    lim.record("acct")  # failures keep being recorded past the limit (this is what the login flow does)
    assert lim.is_blocked("acct")
    assert client.zcard(f"{prefix}rl:login:acct") == 3


@needs_redis
def test_reset_and_clear(client, prefix):
    lim = limiter(client, prefix, max_events=1)
    lim.record("a")
    lim.record("b")
    lim.reset("a")
    assert not lim.is_blocked("a") and lim.is_blocked("b")
    lim.clear()
    assert not lim.is_blocked("b")


@needs_redis
def test_old_events_leave_the_window(client, prefix):
    lim = limiter(client, prefix, max_events=2, window=1.0)
    assert lim.hit("ip") and lim.hit("ip") and not lim.hit("ip")
    time.sleep(1.2)
    assert lim.hit("ip")  # the window moved on


@needs_redis
def test_keys_expire_by_themselves(client, prefix):
    lim = limiter(client, prefix, window=30.0)
    lim.hit("ip")
    ttl = client.pttl(f"{prefix}rl:login:ip")
    assert 0 < ttl <= 30_000  # an idle key is removed by Redis, never leaked


@needs_redis
def test_two_replicas_share_one_limit(client, prefix):
    """The point of this module: attempts made through gateway A count against gateway B."""
    gateway_a, gateway_b = limiter(client, prefix), limiter(client, prefix)
    assert gateway_a.hit("ip") and gateway_b.hit("ip") and gateway_a.hit("ip")
    assert not gateway_b.hit("ip") and not gateway_a.hit("ip")
    assert gateway_b.is_blocked("ip")


@needs_redis
def test_concurrent_hits_never_exceed_the_limit(client, prefix):
    lim = limiter(client, prefix, max_events=10)
    results: list[bool] = []
    guard = threading.Lock()

    def worker():
        for _ in range(10):
            ok = lim.hit("ip")
            with guard:
                results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 80
    assert sum(results) == 10  # atomic in Redis: exactly the allowance, however the calls interleave


@needs_redis
def test_a_rapid_burst_in_the_same_millisecond_is_still_counted(client, prefix):
    lim = limiter(client, prefix, max_events=50)
    assert all(lim.hit("ip") for _ in range(50))  # entries are unique even at identical timestamps
    assert not lim.hit("ip")


# -- fallback behaviour (no Redis needed) -------------------------------------------------


class FailingPrimary:
    """Stands in for a Redis limiter whose server is down."""

    name = "login"

    def __init__(self):
        self.calls = 0
        self.broken = True
        self.store = SlidingWindowLimiter(3, 300)

    def _maybe_fail(self):
        self.calls += 1
        if self.broken:
            raise redis.ConnectionError("down")

    def hit(self, key):
        self._maybe_fail()
        return self.store.hit(key)

    def record(self, key):
        self._maybe_fail()
        self.store.record(key)

    def is_blocked(self, key):
        self._maybe_fail()
        return self.store.is_blocked(key)

    def reset(self, key):
        self._maybe_fail()
        self.store.reset(key)

    def clear(self):
        self._maybe_fail()
        self.store.clear()


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def resilient(primary=None, retry_after=5.0, clock=None):
    return ResilientLimiter(primary or FailingPrimary(), SlidingWindowLimiter(3, 300), retry_after, clock or Clock())


def test_when_redis_is_down_the_in_process_limit_still_protects():
    lim = resilient()
    assert [lim.hit("ip") for _ in range(4)] == [True, True, True, False]
    lim.record("acct")
    lim.record("acct")
    lim.record("acct")
    assert lim.is_blocked("acct")  # no exception ever reaches the request


def test_an_outage_is_not_retried_on_every_request():
    primary, clock = FailingPrimary(), Clock()
    lim = resilient(primary, retry_after=5.0, clock=clock)
    for _ in range(50):
        lim.hit("ip")
    assert primary.calls == 1  # one failed attempt, then straight to the fallback
    assert lim.degraded
    clock.now += 6
    lim.hit("ip")
    assert primary.calls == 2  # retried after the pause


def test_it_returns_to_redis_when_redis_recovers():
    primary, clock = FailingPrimary(), Clock()
    lim = resilient(primary, retry_after=5.0, clock=clock)
    lim.hit("ip")
    assert lim.degraded
    primary.broken = False
    clock.now += 6
    assert lim.hit("ip") is True
    assert not lim.degraded
    assert primary.store.hit("ip")  # the call really went to the shared limiter


def test_outages_are_counted():
    before = backend_errors.labels("login")._value.get()
    resilient().hit("ip")
    assert backend_errors.labels("login")._value.get() == before + 1


def test_reset_clears_both_sides_and_clear_never_raises():
    primary, clock = FailingPrimary(), Clock()
    lim = resilient(primary, clock=clock)
    for _ in range(3):
        lim.record("acct")  # lands in the fallback while Redis is down
    assert lim.is_blocked("acct")
    lim.reset("acct")
    assert not lim.is_blocked("acct")
    lim.clear()  # Redis is down: still no exception


def test_limits_are_visible_to_callers():
    lim = resilient()
    assert (lim.max_events, lim.window) == (3, 300)  # the 429 response reads `.window` for Retry-After


# -- backend selection ------------------------------------------------------------------


def test_backend_defaults_to_memory_without_redis(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_BACKEND", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert ratelimit.rate_limit_backend() == "memory"
    assert isinstance(make_limiter("x", 1, 1), SlidingWindowLimiter)


def test_backend_auto_uses_redis_when_configured(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_BACKEND", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    assert ratelimit.rate_limit_backend() == "redis"
    assert isinstance(make_limiter("x", 1, 1), ResilientLimiter)


def test_backend_can_be_forced_and_rejects_nonsense(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")
    assert isinstance(make_limiter("x", 1, 1), SlidingWindowLimiter)  # explicit memory beats a configured Redis
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "bogus")
    with pytest.raises(ValueError):
        ratelimit.rate_limit_backend()


def test_an_unreachable_redis_does_not_break_a_login_style_flow(monkeypatch):
    """End to end through make_limiter: Redis configured but nothing listening."""
    monkeypatch.delenv("RATE_LIMIT_BACKEND", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # a closed port
    monkeypatch.setattr(ratelimit, "_client", None)
    lim = make_limiter("login_account", max_events=2, window_seconds=300)
    started = time.monotonic()
    assert lim.hit("acct") and lim.hit("acct") and not lim.hit("acct")
    assert time.monotonic() - started < 3  # one fast connection failure, not one per call
    monkeypatch.setattr(ratelimit, "_client", None)
