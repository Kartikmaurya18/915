"""A small TTL cache with single-flight loading for read-only endpoints.

Many users watching the same strike or the same past session ask the engine (or, in gateway mode,
the leading ingestor over Redis) the identical question every few seconds. This collapses them:
within `ttl` the answer is reused, and concurrent misses for one key share a single load instead of
each making their own. It is per process (every gateway keeps its own), which is enough: the load on
the ingestor becomes at most one call per key per `ttl` per gateway, regardless of user count.

Only successful, non-None results are stored; errors and "no data" answers are never cached, so a
strike that has no candles *yet* is retried on the next request.

Cached values are shared: callers must treat them as read-only (copy before changing).
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Hashable


class ReadCache:
    def __init__(self, max_entries: int = 256, clock: Callable[[], float] = time.monotonic):
        self._max = max_entries
        self._clock = clock
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._inflight: dict[Hashable, asyncio.Future] = {}
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        self._data.clear()

    async def get(self, key: Hashable, ttl: float, loader: Callable[[], Awaitable[Any]]) -> Any:
        entry = self._data.get(key)
        if entry is not None and entry[0] > self._clock():
            self._data.move_to_end(key)
            self.hits += 1
            return entry[1]

        pending = self._inflight.get(key)
        if pending is not None:
            self.hits += 1  # joined someone else's load
            return await asyncio.shield(pending)

        self.misses += 1
        task = asyncio.ensure_future(loader())
        self._inflight[key] = task
        try:
            value = await asyncio.shield(task)
        finally:
            if self._inflight.get(key) is task:
                del self._inflight[key]
        if value is not None and ttl > 0:
            self._data[key] = (self._clock() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
        return value
