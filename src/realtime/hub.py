"""Gateway-side fan-out: topic state, per-client delivery, slow-client isolation.

Everything here runs on ONE asyncio event loop and never blocks it:

* `Hub.apply_update` is synchronous and O(subscribers): it stores the new topic state,
  builds the patch and snapshot JSON once (shared by all clients), and marks each
  subscriber's topic "dirty". It awaits nothing, so neither the ingest path nor a fast
  client can ever wait on a slow one.
* Each client has its own task that sends the *current* state of its dirty topics:
  a patch if the client holds exactly the previous version, otherwise a full snapshot.
  A client that cannot keep up therefore skips intermediate versions (coalescing) -
  its memory is bounded by (topics x 1) plus a small control queue, regardless of
  how fast the feed runs. A client that stops reading for SEND_TIMEOUT is disconnected.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from src.realtime import metrics, protocol
from src.realtime.patch import diff
from src.realtime.topics import TopicError, parse_topic


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


HEARTBEAT_SECONDS = _env_float("RT_HEARTBEAT_SECONDS", 15)
SEND_TIMEOUT_SECONDS = _env_float("RT_SEND_TIMEOUT_SECONDS", 5)
MAX_TOPICS_PER_CLIENT = int(os.getenv("RT_MAX_TOPICS_PER_CLIENT", "16"))
CONTROL_QUEUE_MAX = int(os.getenv("RT_CONTROL_QUEUE_MAX", "64"))
# Sockets of a server running without auth have no user id; they all belong to this one implicit user.
LOCAL_USER_KEY = "local-dev"
PAPER_CHANGED_FRAME = protocol.control_message("paper_changed")


@dataclass
class TopicUpdate:
    topic: str
    epoch: int
    seq: int
    ts: int  # source time, epoch ms UTC
    pub: int  # ingestor publish time, epoch ms UTC
    payload: Any


class TopicState:
    """Latest value of one topic plus the pre-serialised frames derived from it."""

    __slots__ = ("topic", "epoch", "seq", "prev_seq", "ts", "pub", "payload", "received", "snapshot_json", "patch_json")

    def __init__(self, update: TopicUpdate, previous: "TopicState | None", received: float):
        self.topic = update.topic
        self.epoch = update.epoch
        self.seq = update.seq
        self.ts = update.ts
        self.pub = update.pub
        self.payload = update.payload
        self.received = received
        self.snapshot_json = protocol.snapshot_message(
            self.topic, self.epoch, self.seq, self.ts, self.pub, self.payload
        )
        self.prev_seq: int | None = None
        self.patch_json: str | None = None
        if previous is not None and previous.epoch == update.epoch:
            node = diff(previous.payload, update.payload)
            frame = protocol.patch_message(
                self.topic, self.epoch, self.seq, previous.seq, self.ts, self.pub,
                node if node is not None else ["d", {}, []],
            )
            self.prev_seq = previous.seq  # adjacency: the version this patch applies to
            # A patch is only worth sending if it is actually smaller than the snapshot.
            if len(frame) < len(self.snapshot_json):
                self.patch_json = frame


class ClientSession:
    def __init__(
        self,
        hub: "Hub",
        send: Callable[[str], Awaitable[None]],
        close: Callable[[int, str], Awaitable[None]],
        user_id: str | None = None,
    ):
        self.hub = hub
        self._send_frame = send
        self._close = close
        self.user_id = user_id
        self.subs: set[str] = set()
        self.sent: dict[str, tuple[int, int]] = {}
        self._dirty: dict[str, None] = {}
        self._force_full: set[str] = set()
        self._control: deque[str] = deque()
        self._paper_pending = False  # at most one "your paper book changed" notice is ever queued
        self._event = asyncio.Event()
        self.closed = False
        self.close_reason: str | None = None
        self.stats = Counter()
        self._task: asyncio.Task | None = None

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    def kill(self, reason: str, code: int = protocol.CLOSE_SLOW) -> None:
        """Disconnect this client. Safe to call from anywhere on the loop, any number of times."""
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        metrics.clients_dropped.labels(reason).inc()
        self.hub._remove(self)
        self._event.set()
        asyncio.get_running_loop().create_task(self._close(code, reason))

    async def shutdown(self) -> None:
        """Called when the socket has already gone (disconnect)."""
        if not self.closed:
            self.closed = True
            self.hub._remove(self)
            self._event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # -- state changes (called by the hub / the websocket reader) --------------

    def mark_dirty(self, topic: str) -> None:
        if self.closed:
            return
        if topic in self._dirty:
            self.stats["coalesced"] += 1  # an older, still-unsent version is being superseded
            metrics.coalesced.inc()
        else:
            self._dirty[topic] = None
        self._event.set()

    def notify_paper(self) -> None:
        """Tell this client its own paper book changed so it can refetch. Notices coalesce: any number
        of changes before the next send produce one frame, so a busy account cannot flood the socket
        or overflow the control queue."""
        if self.closed or self._paper_pending:
            return
        self._paper_pending = True
        self._event.set()

    def queue_control(self, frame: str) -> None:
        if self.closed:
            return
        if len(self._control) >= CONTROL_QUEUE_MAX:
            self.kill("control_overflow")
            return
        self._control.append(frame)
        self._event.set()

    def subscribe(self, topics: list[str]) -> list[str]:
        accepted = []
        for topic in topics:
            if topic in self.subs:
                accepted.append(topic)
                continue
            if len(self.subs) >= MAX_TOPICS_PER_CLIENT:
                self.queue_control(protocol.error_message("too_many_topics", f"limit is {MAX_TOPICS_PER_CLIENT}", topic))
                continue
            self.subs.add(topic)
            self.hub._index(topic, self)
            self._force_full.add(topic)  # a new subscriber always starts from a snapshot
            self.sent.pop(topic, None)
            accepted.append(topic)
            if topic in self.hub.states:
                self.mark_dirty(topic)
        metrics.subscriptions.set(self.hub.subscription_count())
        return accepted

    def unsubscribe(self, topics: list[str]) -> list[str]:
        removed = []
        for topic in topics:
            if topic in self.subs:
                self.subs.discard(topic)
                self.hub._unindex(topic, self)
                self._dirty.pop(topic, None)
                self._force_full.discard(topic)
                self.sent.pop(topic, None)
                removed.append(topic)
        metrics.subscriptions.set(self.hub.subscription_count())
        return removed

    def request_resync(self, topic: str) -> bool:
        if topic not in self.subs:
            return False
        self._force_full.add(topic)
        if topic in self.hub.states:
            self.mark_dirty(topic)
        return True

    # -- delivery --------------------------------------------------------------

    async def _send(self, frame: str, kind: str) -> bool:
        try:
            await asyncio.wait_for(self._send_frame(frame), SEND_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self.kill("slow_consumer")
            return False
        except Exception:
            self.kill("send_failed", code=1011)
            return False
        self.stats["frames"] += 1
        self.stats["bytes"] += len(frame)
        metrics.messages_sent.labels(kind).inc()
        metrics.bytes_sent.labels(kind).inc(len(frame))
        return True

    def _next_frame(self, topic: str) -> tuple[str, str, "TopicState"] | None:
        """The frame that brings this client up to date on `topic`, or None if it is."""
        state = self.hub.states.get(topic)
        if state is None or topic not in self.subs:
            return None
        sent = self.sent.get(topic)
        if sent == (state.epoch, state.seq) and topic not in self._force_full:
            return None  # up to date - unless the client explicitly asked for a fresh snapshot
        same_epoch = sent is not None and sent[0] == state.epoch
        adjacent = same_epoch and state.prev_seq == sent[1]
        if adjacent and state.patch_json is not None and topic not in self._force_full:
            return state.patch_json, "patch", state

        # Everything else is a full snapshot; record why.
        if topic in self._force_full:
            reason = "subscribe" if sent is None else "resync"
        elif sent is None:
            reason = "subscribe"
        elif not same_epoch:
            reason = "epoch"
        elif adjacent:
            reason = "patch_larger"
        else:
            reason = "behind"
            skipped = max(0, state.seq - sent[1] - 1)
            if skipped:
                self.stats["coalesced"] += skipped
                metrics.coalesced.inc(skipped)
        metrics.snapshots_sent.labels(reason).inc()
        return state.snapshot_json, "snapshot", state

    async def _run(self) -> None:
        try:
            while not self.closed:
                try:
                    await asyncio.wait_for(self._event.wait(), HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    if not await self._send(self.hub.heartbeat_frame(), "heartbeat"):
                        return
                    continue
                self._event.clear()
                while self._control and not self.closed:
                    if not await self._send(self._control.popleft(), "control"):
                        return
                if self._paper_pending and not self.closed:
                    self._paper_pending = False  # cleared first: a change during the send queues the next notice
                    metrics.paper_notices.inc()
                    if not await self._send(PAPER_CHANGED_FRAME, "control"):
                        return
                while self._dirty and not self.closed:
                    topic = next(iter(self._dirty))
                    del self._dirty[topic]
                    built = self._next_frame(topic)
                    if built is None:
                        continue
                    frame, kind, state = built
                    if not await self._send(frame, kind):
                        return
                    self.sent[topic] = (state.epoch, state.seq)
                    self._force_full.discard(topic)
                    now = time.monotonic()
                    metrics.fanout_seconds.observe(now - state.received)
                    metrics.publish_to_send_seconds.observe(max(0.0, time.time() - state.pub / 1000.0))
        except asyncio.CancelledError:
            raise


class Hub:
    def __init__(self, max_clients: int | None = None, max_clients_per_user: int | None = None):
        self.states: dict[str, TopicState] = {}
        self._subscribers: dict[str, set[ClientSession]] = {}
        self.clients: set[ClientSession] = set()
        self._per_user: Counter = Counter()
        self._by_user: dict[str, set[ClientSession]] = {}
        self.max_clients = max_clients
        self.max_clients_per_user = max_clients_per_user
        self.last_update: float | None = None
        metrics.feed_age_seconds.set_function(lambda: (time.monotonic() - self.last_update) if self.last_update else -1)

    # -- ingest ----------------------------------------------------------------

    def apply_update(self, update: TopicUpdate) -> str:
        current = self.states.get(update.topic)
        key, held = (update.epoch, update.seq), (current.epoch, current.seq) if current else None
        if held is not None and key == held:
            metrics.updates.labels("duplicate").inc()
            return "duplicate"
        if held is not None and key < held:
            metrics.updates.labels("out_of_order").inc()
            return "out_of_order"
        now = time.monotonic()
        state = TopicState(update, current, now)
        self.states[update.topic] = state
        self.last_update = now
        metrics.updates.labels("applied").inc()
        metrics.hub_topics.set(len(self.states))
        for client in tuple(self._subscribers.get(update.topic, ())):
            client.mark_dirty(update.topic)
        return "applied"

    def apply_updates(self, updates: list[TopicUpdate]) -> None:
        for update in updates:
            self.apply_update(update)

    def latest(self, topic: str) -> Any | None:
        state = self.states.get(topic)
        return state.payload if state else None

    def feed_age_ms(self) -> int | None:
        return None if self.last_update is None else int((time.monotonic() - self.last_update) * 1000)

    def heartbeat_frame(self) -> str:
        return protocol.control_message("heartbeat", t=int(time.time() * 1000), feed_age_ms=self.feed_age_ms())

    # -- clients ---------------------------------------------------------------

    def admit(self, user_id: str | None) -> str | None:
        """None if a new client may connect, else the rejection reason."""
        if self.max_clients is not None and len(self.clients) >= self.max_clients:
            return "server_at_capacity"
        if user_id is not None and self.max_clients_per_user is not None and self._per_user[user_id] >= self.max_clients_per_user:
            return "too_many_connections"
        return None

    def add_client(
        self,
        send: Callable[[str], Awaitable[None]],
        close: Callable[[int, str], Awaitable[None]],
        user_id: str | None = None,
    ) -> ClientSession:
        client = ClientSession(self, send, close, user_id)
        self.clients.add(client)
        if user_id is not None:
            self._per_user[user_id] += 1
        self._by_user.setdefault(user_id if user_id is not None else LOCAL_USER_KEY, set()).add(client)
        metrics.ws_clients.set(len(self.clients))
        client.start()
        return client

    def notify_user(self, user_id: str) -> int:
        """Send a paper_changed notice to every connected socket of one user. Returns how many were
        notified. Other users' sockets are never touched."""
        sockets = tuple(self._by_user.get(user_id, ()))
        for client in sockets:
            client.notify_paper()
        return len(sockets)

    def _remove(self, client: ClientSession) -> None:
        key = client.user_id if client.user_id is not None else LOCAL_USER_KEY
        bucket = self._by_user.get(key)
        if bucket is not None:
            bucket.discard(client)
            if not bucket:
                del self._by_user[key]
        if client in self.clients:
            self.clients.discard(client)
            if client.user_id is not None:
                self._per_user[client.user_id] -= 1
                if self._per_user[client.user_id] <= 0:
                    del self._per_user[client.user_id]
        for topic in tuple(client.subs):
            self._unindex(topic, client)
        metrics.ws_clients.set(len(self.clients))
        metrics.subscriptions.set(self.subscription_count())

    def _index(self, topic: str, client: ClientSession) -> None:
        self._subscribers.setdefault(topic, set()).add(client)

    def _unindex(self, topic: str, client: ClientSession) -> None:
        bucket = self._subscribers.get(topic)
        if bucket is not None:
            bucket.discard(client)
            if not bucket:
                del self._subscribers[topic]

    def subscription_count(self) -> int:
        return sum(len(b) for b in self._subscribers.values())

    async def close_all(self, reason: str = "server_shutdown") -> None:
        for client in tuple(self.clients):
            client.kill(reason, code=1001)
        await asyncio.sleep(0)


def validate_topics(raw: Any) -> tuple[list[str], list[tuple[str, str]]]:
    """Split a client-supplied topic list into (valid, [(topic, error), ...])."""
    if not isinstance(raw, list) or len(raw) > 64:
        return [], [("", "topics must be a list of at most 64 entries")]
    valid, errors = [], []
    for topic in raw:
        try:
            parse_topic(topic)
            valid.append(topic)
        except TopicError as exc:
            errors.append((str(topic)[:40], str(exc)))
    return list(dict.fromkeys(valid)), errors
