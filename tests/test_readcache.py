"""ReadCache: TTL reuse, single-flight loading, and what must never be cached."""

from __future__ import annotations

import asyncio

import pytest

from src.api.readcache import ReadCache


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def run(coro):
    return asyncio.run(coro)


def test_reuses_a_value_within_ttl_and_reloads_after():
    clock = Clock()
    cache = ReadCache(clock=clock)
    calls = []

    async def loader():
        calls.append(1)
        return {"n": len(calls)}

    async def scenario():
        a = await cache.get("k", 2.0, loader)
        b = await cache.get("k", 2.0, loader)
        clock.now += 2.5
        c = await cache.get("k", 2.0, loader)
        return a, b, c

    a, b, c = run(scenario())
    assert a == b == {"n": 1}
    assert c == {"n": 2}
    assert len(calls) == 2


def test_concurrent_misses_share_one_load():
    cache = ReadCache()
    calls = []

    async def loader():
        calls.append(1)
        await asyncio.sleep(0.05)
        return "value"

    async def scenario():
        return await asyncio.gather(*(cache.get("k", 5.0, loader) for _ in range(50)))

    results = run(scenario())
    assert results == ["value"] * 50
    assert len(calls) == 1


def test_keys_are_independent():
    cache = ReadCache()

    async def scenario():
        a = await cache.get(("s", 24800.0), 5.0, lambda: asyncio.sleep(0, "A"))
        b = await cache.get(("s", 24850.0), 5.0, lambda: asyncio.sleep(0, "B"))
        return a, b

    assert run(scenario()) == ("A", "B")


def test_none_results_are_not_cached():
    cache = ReadCache()
    calls = []

    async def loader():
        calls.append(1)
        return None

    async def scenario():
        await cache.get("k", 5.0, loader)
        await cache.get("k", 5.0, loader)

    run(scenario())
    assert len(calls) == 2  # "no data yet" is retried, not remembered


def test_errors_propagate_to_every_waiter_and_are_not_cached():
    cache = ReadCache()
    calls = []

    async def loader():
        calls.append(1)
        await asyncio.sleep(0.02)
        raise ValueError("boom")

    async def scenario():
        results = await asyncio.gather(*(cache.get("k", 5.0, loader) for _ in range(5)), return_exceptions=True)
        again = await asyncio.gather(cache.get("k", 5.0, loader), return_exceptions=True)
        return results, again

    results, again = run(scenario())
    assert all(isinstance(r, ValueError) for r in results)
    assert len(calls) == 2  # one shared failed load, then a fresh attempt
    assert isinstance(again[0], ValueError)
    assert len(cache) == 0


def test_zero_ttl_never_stores():
    cache = ReadCache()

    async def scenario():
        await cache.get("k", 0, lambda: asyncio.sleep(0, "v"))

    run(scenario())
    assert len(cache) == 0


def test_evicts_the_least_recently_used_entry():
    cache = ReadCache(max_entries=2)

    async def scenario():
        for key in ("a", "b"):
            await cache.get(key, 60, lambda k=key: asyncio.sleep(0, k))
        await cache.get("a", 60, lambda: asyncio.sleep(0, "x"))  # touch "a" so "b" is the oldest
        await cache.get("c", 60, lambda: asyncio.sleep(0, "c"))

    run(scenario())
    assert len(cache) == 2
    assert set(k for k in cache._data) == {"a", "c"}


def test_a_cancelled_first_caller_does_not_break_the_others():
    cache = ReadCache()

    async def loader():
        await asyncio.sleep(0.1)
        return "ok"

    async def scenario():
        first = asyncio.ensure_future(cache.get("k", 5.0, loader))
        await asyncio.sleep(0.01)
        second = asyncio.ensure_future(cache.get("k", 5.0, loader))
        await asyncio.sleep(0.01)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        return await second

    assert run(scenario()) == "ok"
