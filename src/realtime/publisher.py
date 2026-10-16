"""Turns engine snapshots into versioned topic updates.

Runs wherever the engine runs (the ingestor, or the single process in embedded mode).
It owns the per-topic sequence numbers and suppresses topics whose payload did not
change, so every published (epoch, seq) is a real change.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Callable

from src.realtime.hub import TopicUpdate
from src.realtime.topics import split_snapshot


def _source_ms(snapshot: dict) -> int:
    raw = snapshot.get("timestamp")
    if isinstance(raw, str):
        try:
            return int(datetime.fromisoformat(raw).timestamp() * 1000)
        except ValueError:
            pass
    return int(time.time() * 1000)


class TopicPublisher:
    def __init__(self, sink: Callable[[list[TopicUpdate]], None], underlying: str, epoch: int = 1):
        self._sink = sink
        self._underlying = underlying
        self._epoch = epoch
        self._seq: dict[str, int] = {}
        self._last: dict[str, object] = {}
        self._lock = threading.Lock()

    @property
    def epoch(self) -> int:
        return self._epoch

    def new_epoch(self, epoch: int) -> None:
        """A new leader (or a restart) starts a fresh sequence. Forget what was published
        so the first publish of every topic is a full snapshot for everyone."""
        with self._lock:
            self._epoch = epoch
            self._seq.clear()
            self._last.clear()

    def build(self, snapshot: dict) -> list[TopicUpdate]:
        topics = split_snapshot(snapshot, self._underlying)
        source, now = _source_ms(snapshot), int(time.time() * 1000)
        updates = []
        with self._lock:
            for topic, payload in topics.items():
                if topic in self._last and self._last[topic] == payload:
                    continue
                self._last[topic] = payload
                seq = self._seq[topic] = self._seq.get(topic, 0) + 1
                updates.append(TopicUpdate(topic, self._epoch, seq, source, now, payload))
        return updates

    def on_snapshot(self, snapshot: dict) -> list[TopicUpdate]:
        updates = self.build(snapshot)
        if updates:
            self._sink(updates)
        return updates
