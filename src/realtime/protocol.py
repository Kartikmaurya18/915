"""Wire protocol v1 for /ws/v1 (JSON text frames).

Server -> client
    hello       {session, heartbeat_s, max_topics}
    subscribed  {topics}                 unsubscribed {topics}
    snapshot    {topic, epoch, seq, ts, pub, data}
    patch       {topic, epoch, seq, base, ts, pub, patch}   apply only if you hold `base`
    heartbeat   {t, feed_age_ms}         feed_age_ms: time since the gateway last heard
                                          ANY market update (None before the first one).
                                          Grows when the ingestor or Redis is down.
    error       {code, message, topic?}
    rate_limited {retry_after_s}

    batch       {frames: [snapshot|patch, ...]}   only if the client asked for it (subscribe with batch=true):
                                          all topic updates of one tick in a single message

Client -> server
    auth        {token}                  first frame when auth is required
    subscribe   {topics, batch?}   unsubscribe {topics}      batch=true opts in to `batch` frames
    resync      {topic}                  request a fresh snapshot (e.g. after a gap)
    ping        {}

Ordering: within a topic, (epoch, seq) increases strictly. `epoch` changes when a new
ingestor becomes leader; a client must treat a new epoch as a fresh snapshot.
`ts` is the source (exchange/engine tick) time and `pub` the ingestor's publish time,
both epoch milliseconds in UTC.
"""

from __future__ import annotations

import json
from typing import Any

VERSION = 1

# Close codes (4000-4999 are application-defined)
CLOSE_AUTH = 4401
CLOSE_CAPACITY = 4429
CLOSE_SLOW = 4408
CLOSE_PROTOCOL = 4400


def dumps(message: dict) -> str:
    return json.dumps(message, separators=(",", ":"))


def snapshot_message(topic: str, epoch: int, seq: int, ts: int, pub: int, data: Any) -> str:
    return dumps({"v": VERSION, "type": "snapshot", "topic": topic, "epoch": epoch, "seq": seq, "ts": ts, "pub": pub, "data": data})


def patch_message(topic: str, epoch: int, seq: int, base: int, ts: int, pub: int, patch: Any) -> str:
    return dumps({"v": VERSION, "type": "patch", "topic": topic, "epoch": epoch, "seq": seq, "base": base, "ts": ts, "pub": pub, "patch": patch})


def batch_message(frames: list[str]) -> str:
    """Several already-serialised frames in ONE websocket message, in order. Concatenated rather than
    re-encoded, so building it costs nothing per topic; a client unpacks and handles each inner frame
    exactly as if it had arrived on its own."""
    return '{"v":%d,"type":"batch","frames":[%s]}' % (VERSION, ",".join(frames))


def control_message(type_: str, **fields: Any) -> str:
    return dumps({"v": VERSION, "type": type_, **fields})


def error_message(code: str, message: str, topic: str | None = None) -> str:
    fields: dict[str, Any] = {"code": code, "message": message}
    if topic is not None:
        fields["topic"] = topic
    return control_message("error", **fields)
