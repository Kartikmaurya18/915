"""The ingestor: the only process that talks to the market-data provider.

    python -m src.ingestor

Run two or more for high availability. They contend for a Redis lease; exactly one - the
leader - runs the engine (one provider connection), the paper-trading exit worker, and the
control-channel server, and publishes every tick as versioned topic updates. The others sit
idle ("standby") and take over if the leader stops renewing its lease.

Leadership is protected by a fencing token (see src/realtime/redis_bus.py): even a leader
that stalls past its lease and wakes up still believing it leads cannot publish, because
Redis rejects its stale token - and that rejection makes it step down immediately.

Known limit: in-progress candle state lives in the leader's memory, so a failover restarts
the current candle on the new leader. Completed candles are in the history store.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
import uuid

import redis

from src.auth import settings as auth_settings
from src.config import load_config
from src.engine import MarketEngine
from src.paper.exit_worker import PaperExitWorker
from src.realtime import metrics
from src.realtime.control import ControlServer
from src.realtime.publisher import TopicPublisher
from src.realtime.redis_bus import LeaderElector, RedisSink, default_prefix, redis_url

log = logging.getLogger("market_data.ingestor")


class Ingestor:
    def __init__(
        self,
        url: str | None = None,
        prefix: str | None = None,
        node_id: str | None = None,
        ttl_ms: int | None = None,
        cfg: dict | None = None,
    ):
        self.node_id = node_id or os.getenv("INGESTOR_ID") or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.ttl_ms = ttl_ms or int(os.getenv("INGESTOR_LEASE_TTL_MS", "6000"))
        self.prefix = prefix if prefix is not None else default_prefix()
        self.url = url or redis_url()
        self.engine = MarketEngine(cfg or load_config())
        self.client = redis.Redis.from_url(self.url, decode_responses=True, client_name=f"rt-ingestor-{self.node_id}")
        self.elector = LeaderElector(self.client, self.node_id, self.prefix, self.ttl_ms)
        self.publisher = TopicPublisher(RedisSink(self.client, self.elector, self.prefix), self.engine.underlying, epoch=0)
        self.control = ControlServer(self.engine, self.elector, self.url, self.prefix)
        self.exit_worker = PaperExitWorker() if auth_settings.auth_required() else None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.became_leader = threading.Event()  # tests / diagnostics

    # -- engine callback (runs on the engine's tick thread) --------------------

    def _on_snapshot(self, snapshot: dict) -> None:
        try:
            self.publisher.on_snapshot(snapshot)
        except Exception:
            metrics.publishes.labels("error").inc()
            log.exception("publish failed")
        if self.elector.lost:
            self._wake.set()  # Redis said we are no longer the leader: react now, not at the next lease check

    # -- leadership transitions ------------------------------------------------

    def _become_leader(self, fence: int) -> None:
        log.info("node %s is now leader (epoch %s)", self.node_id, fence)
        self.publisher.new_epoch(fence)
        self.engine.unsubscribe(self._on_snapshot)
        self.engine.subscribe(self._on_snapshot)
        if self.exit_worker is not None:
            self.engine.add_chain_listener(self.exit_worker)
        self.engine.start()
        self.became_leader.set()

    def _step_down(self, reason: str) -> None:
        log.warning("node %s stepping down (%s)", self.node_id, reason)
        self.engine.stop()  # releases the provider connection; a standby opens its own
        self.engine.unsubscribe(self._on_snapshot)
        if self.exit_worker is not None:
            self.engine.remove_chain_listener(self.exit_worker)
        self.elector.step_down(reason)
        self.became_leader.clear()  # only once EVERYTHING has stopped, so observers can trust it

    # -- main loop -------------------------------------------------------------

    def run_forever(self) -> None:
        self.control.start()
        renew_every = max(0.2, self.ttl_ms / 3000.0)
        while not self._stop.is_set():
            if self.elector.fence is None:
                fence = self.elector.try_acquire()
                if fence is not None:
                    self._become_leader(fence)
                interval = renew_every if fence is not None else 0.5  # standbys poll often: takeover latency
            else:
                if self.elector.lost:
                    self._step_down("fenced")
                elif not self.elector.renew():
                    self._step_down("lease_lost")
                interval = renew_every
            self._wake.wait(interval)
            self._wake.clear()
        self.shutdown()

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def shutdown(self) -> None:
        self.control.stop()
        if self.elector.fence is not None:
            self._step_down("shutdown")  # stop the engine first: never two live provider connections
        self.elector.release()  # then hand the lease over now instead of making a standby wait out the TTL
        self.engine.close()
        try:
            self.client.close()
        except redis.RedisError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.ingestor", description=__doc__.split("\n\n")[0])
    parser.add_argument("--node-id", default=None)
    parser.add_argument("--ttl-ms", type=int, default=None)
    parser.add_argument("--metrics-port", type=int, default=int(os.getenv("INGESTOR_METRICS_PORT", "9100")))
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    auth_settings.validate_startup_config()
    if args.metrics_port:
        from prometheus_client import start_http_server

        start_http_server(args.metrics_port)  # also serves as the container liveness probe

    ingestor = Ingestor(node_id=args.node_id, ttl_ms=args.ttl_ms)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: ingestor.request_stop())
        except (ValueError, OSError):  # not on the main thread / unsupported signal on this platform
            pass
    ingestor.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
