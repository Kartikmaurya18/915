"""Reference client-side state machine for the /ws/v1 protocol.

Used by the tests and the load generator, and written to be ported line-for-line to
the browser's WebSocket manager. It holds the last (epoch, seq, payload) per topic and
decides, frame by frame, whether it is still in sync.
"""

from __future__ import annotations

from typing import Any

from src.realtime.patch import apply_patch


class TopicMirror:
    def __init__(self) -> None:
        self.topics: dict[str, tuple[int, int, Any]] = {}
        self.resyncs = 0

    def payload(self, topic: str) -> Any:
        entry = self.topics.get(topic)
        return entry[2] if entry else None

    def version(self, topic: str) -> tuple[int, int] | None:
        entry = self.topics.get(topic)
        return (entry[0], entry[1]) if entry else None

    def apply(self, frame: dict) -> str:
        """Apply one server frame. Returns "ok", "ignored", or "resync" (the caller must
        then send {"type": "resync", "topic": ...} - never keep applying patches)."""
        kind = frame.get("type")
        if kind == "snapshot":
            held = self.topics.get(frame["topic"])
            if held is not None and (frame["epoch"], frame["seq"]) < (held[0], held[1]):
                return "ignored"  # an older snapshot overtaken in flight
            self.topics[frame["topic"]] = (frame["epoch"], frame["seq"], frame["data"])
            return "ok"
        if kind == "patch":
            held = self.topics.get(frame["topic"])
            if held is None or held[0] != frame["epoch"] or held[1] != frame["base"]:
                self.resyncs += 1
                return "resync"  # a gap: we do not hold the version this patch applies to
            self.topics[frame["topic"]] = (frame["epoch"], frame["seq"], apply_patch(held[2], frame["patch"]))
            return "ok"
        return "ignored"
