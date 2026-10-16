"""Gateway -> ingestor request/response over Redis lists.

Some endpoints need the engine itself (live settings, the log ring buffer, strike candle
builders, the market-history store). A gateway does not own the engine, so it asks the
current leader. Only the leader's control thread pops requests, so exactly one node
answers; if there is no leader the caller times out with ControlUnavailable (HTTP 503).

Only the methods in ALLOWED can be invoked, with JSON arguments. The channel carries no
user identity and performs no authorization: the gateway endpoint that calls it has
already authenticated and authorised the caller. Redis must therefore be reachable only
from the gateways and ingestors (private network + password/TLS).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any

import redis
import redis.asyncio as aioredis

from src.realtime import metrics
from src.realtime.redis_bus import default_prefix, redis_url

log = logging.getLogger("market_data.control")

ALLOWED = frozenset(
    {
        "get_settings",
        "set_atm_config",
        "set_candle_price_basis",
        "reconfigure_source",
        "get_recent_logs",
        "get_persisted_candles",
        "get_persisted_option_chain_snapshots",
        "get_available_trading_dates",
        "get_historical_session",
        "get_persisted_strike_candles_for_date",
        "get_strike_straddle_candles",
    }
)
MAX_REQUEST_AGE_SECONDS = 15.0
RESPONSE_TTL_SECONDS = 30


class ControlUnavailable(RuntimeError):
    """No ingestor answered in time (none is leading, or it is overloaded)."""


class ControlRemoteError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _req_key(prefix: str) -> str:
    return f"{prefix}ctl:req"


def _resp_key(prefix: str, request_id: str) -> str:
    return f"{prefix}ctl:resp:{request_id}"


class ControlClient:
    """Async client used by gateway endpoints."""

    def __init__(self, url: str | None = None, prefix: str | None = None, timeout: float = 5.0, max_in_flight: int = 32):
        self.url = url or redis_url()
        self.prefix = prefix if prefix is not None else default_prefix()
        self.timeout = timeout
        self._client: aioredis.Redis | None = None
        self._slots = asyncio.Semaphore(max_in_flight)

    def _redis(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(self.url, decode_responses=True, client_name="rt-control-client")
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if method not in ALLOWED:
            raise ValueError(f"control method {method!r} is not allowed")
        try:
            await asyncio.wait_for(self._slots.acquire(), 1.0)
        except asyncio.TimeoutError:
            metrics.control_errors.labels("overloaded").inc()
            raise ControlUnavailable("too many requests in flight to the ingestor") from None
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        try:
            client = self._redis()
            body = json.dumps({"id": request_id, "m": method, "a": list(args), "k": kwargs, "t": time.time()})
            await client.rpush(_req_key(self.prefix), body)
            await client.expire(_req_key(self.prefix), 60)  # never leave an unbounded queue behind
            item = await client.blpop([_resp_key(self.prefix, request_id)], timeout=self.timeout)
        except (redis.RedisError, OSError) as exc:
            metrics.control_errors.labels("redis").inc()
            raise ControlUnavailable(f"control channel unavailable: {type(exc).__name__}") from exc
        finally:
            self._slots.release()
        if item is None:
            metrics.control_errors.labels("timeout").inc()
            raise ControlUnavailable("no ingestor answered (none is leading, or it is overloaded)")
        metrics.control_seconds.labels(method).observe(time.perf_counter() - started)
        reply = json.loads(item[1])
        if "error" in reply:
            raise ControlRemoteError(reply["error"]["type"], reply["error"]["message"])
        return reply["result"]


class ControlServer(threading.Thread):
    """Runs in the ingestor: answers requests, but only while this node is the leader."""

    def __init__(self, engine, elector, url: str | None = None, prefix: str | None = None):
        super().__init__(name="control-server", daemon=True)
        self.engine = engine
        self.elector = elector
        self.prefix = prefix if prefix is not None else default_prefix()
        self._client = redis.Redis.from_url(url or redis_url(), decode_responses=True, client_name="rt-control-server")
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            if not self.elector.is_leader:
                self._stop.wait(0.2)
                continue
            try:
                item = self._client.blpop([_req_key(self.prefix)], timeout=1)
            except redis.RedisError:
                self._stop.wait(0.5)
                continue
            if item is None:
                continue
            # Leadership may have ended while we sat blocked in BLPOP. A non-leader must never
            # answer: put the request back at the FRONT of the queue for the real leader.
            if self._stop.is_set() or not self.elector.is_leader:
                try:
                    self._client.lpush(_req_key(self.prefix), item[1])
                except redis.RedisError:
                    pass
                continue
            self._handle(item[1])

    def _handle(self, raw: str) -> None:
        try:
            message = json.loads(raw)
            request_id, method = message["id"], message["m"]
        except (ValueError, KeyError, TypeError):
            return
        if time.time() - float(message.get("t", 0)) > MAX_REQUEST_AGE_SECONDS:
            return  # the caller has long since given up
        try:
            if method not in ALLOWED:
                raise ValueError(f"control method {method!r} is not allowed")
            result = getattr(self.engine, method)(*message.get("a", []), **message.get("k", {}))
            reply = json.dumps({"result": result}, default=str)
        except Exception as exc:  # reported to the caller, never fatal to the server
            reply = json.dumps({"error": {"type": type(exc).__name__, "message": str(exc)}})
        try:
            key = _resp_key(self.prefix, request_id)
            pipe = self._client.pipeline(transaction=False)
            pipe.rpush(key, reply)
            pipe.expire(key, RESPONSE_TTL_SECONDS)
            pipe.execute()
        except redis.RedisError:
            log.warning("could not deliver control reply for %s", method)
