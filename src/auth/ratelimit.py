"""In-process sliding-window rate limiter.

Per-process only: with several gateway replicas each keeps its own counts, so
the effective limit is multiplied by the replica count. Phase 2 moves this to
Redis; the interface below is the seam for that swap.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable


class SlidingWindowLimiter:
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
