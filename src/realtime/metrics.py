"""Prometheus metrics for the real-time path."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

_LATENCY_BUCKETS = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

ws_clients = Gauge("rt_ws_clients", "Connected /ws/v1 clients")
subscriptions = Gauge("rt_subscriptions", "Active topic subscriptions across all clients")
hub_topics = Gauge("rt_hub_topics", "Topics with a current state in this gateway")
feed_age_seconds = Gauge("rt_feed_age_seconds", "Seconds since this gateway last received any market update")

updates = Counter("rt_updates_total", "Topic updates received by the hub", ["result"])  # applied|duplicate|out_of_order
messages_sent = Counter("rt_messages_sent_total", "Frames sent to clients", ["type"])
bytes_sent = Counter("rt_bytes_sent_total", "Payload bytes sent to clients", ["type"])
snapshots_sent = Counter("rt_snapshots_sent_total", "Full snapshots sent", ["reason"])
coalesced = Counter("rt_coalesced_total", "Intermediate topic versions skipped for clients that were behind")
clients_dropped = Counter("rt_clients_dropped_total", "Clients disconnected by the server", ["reason"])
inbound_rejected = Counter("rt_inbound_rejected_total", "Client frames rejected", ["reason"])

fanout_seconds = Histogram(
    "rt_fanout_seconds", "Hub receipt of an update to that frame being handed to the socket", buckets=_LATENCY_BUCKETS
)
publish_to_send_seconds = Histogram(
    "rt_publish_to_send_seconds",
    "Ingestor publish time to frame sent (wall clocks: only meaningful when hosts are clock-synchronised)",
    buckets=_LATENCY_BUCKETS,
)

publishes = Counter("rt_publish_total", "Ingestor publishes", ["result"])  # ok|fenced|error|skipped
publish_seconds = Histogram("rt_publish_seconds", "Time to publish one topic to the bus", buckets=_LATENCY_BUCKETS)
redis_up = Gauge("rt_redis_up", "1 if this process currently has a working Redis connection")
redis_reconnects = Counter("rt_redis_reconnects_total", "Redis subscriber reconnections")
is_leader = Gauge("rt_is_leader", "1 if this ingestor currently holds the leader lease")
leader_changes = Counter("rt_leader_changes_total", "Leadership acquisitions and losses", ["event"])
control_seconds = Histogram("rt_control_rpc_seconds", "Gateway -> ingestor RPC round trip", ["method"], buckets=_LATENCY_BUCKETS)
control_errors = Counter("rt_control_rpc_errors_total", "Control RPC failures", ["reason"])
