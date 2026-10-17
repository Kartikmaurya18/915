from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from src.api.auth_routes import router as auth_router
from src.api.readcache import ReadCache
from src.api.spa import install_spa, spa_dist
from src.auth import settings as auth_settings
from src.auth.deps import AuthUser, authenticate_token, current_user, require_admin
from src.auth.security import AuthError, decode_access_token
from src.config import load_config
from src.engine import MarketEngine
from src.paper.exit_worker import PaperExitWorker
from src.paper.market_view import HubMarketView
from src.paper.service import PaperError, PaperService, default_config
from src.realtime import hub as hub_module
from src.realtime import metrics as rt_metrics
from src.realtime import protocol as rt_protocol
from src.realtime import settings as rt_settings
from src.realtime.control import ControlClient, ControlRemoteError, ControlUnavailable
from src.realtime.hub import Hub, TopicUpdate, validate_topics
from src.realtime.publisher import TopicPublisher
from src.realtime.redis_bus import RedisSubscriber, publish_user_event
from src.realtime.topics import merge_topics
from src.serializers import summarize_persisted_paper_trades
from src.timeutil import today_ist

ROOT = Path(__file__).resolve().parent.parent.parent
IS_SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", "65536"))
WS_AUTH_TIMEOUT_SECONDS = float(os.getenv("WS_AUTH_TIMEOUT_SECONDS", "5"))
WS_MAX_MESSAGE_BYTES = 4096  # clients only ever send a tiny auth/ping frame

def _engine_config() -> dict:
    cfg = load_config()
    if rt_settings.is_gateway():
        # A gateway never ticks and must not open (or write) the ingestor's history database.
        cfg["persistence"] = {**(cfg.get("persistence") or {}), "enabled": False}
    return cfg


engine = MarketEngine(_engine_config())
_ws_clients: set[WebSocket] = set()
_ws_per_user: Counter[str] = Counter()
_loop: asyncio.AbstractEventLoop | None = None
_paper_service: PaperService | None = None
_exit_worker: PaperExitWorker | None = None
_redis_subscriber: RedisSubscriber | None = None
_control: ControlClient | None = None

# Real-time distribution (/ws/v1). The hub is fed either by this process's own engine
# (embedded mode, below) or by Redis (gateway mode).
rt_hub = Hub()


def _publish_to_hub(updates: list[TopicUpdate]) -> None:
    """Called on the engine's tick thread; hand the updates to the event loop."""
    loop = _loop
    if loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(rt_hub.apply_updates, updates)
    except RuntimeError:  # loop shut down between the check and the call
        pass


_publisher = TopicPublisher(_publish_to_hub, engine.underlying)


def get_paper_service() -> PaperService:
    """The per-user paper service (AUTH_MODE=required). Stateless: safe in every gateway replica."""
    global _paper_service
    if _paper_service is None:
        _paper_service = PaperService(HubMarketView(lambda: rt_hub, engine.underlying), default_config(engine.cfg))
    return _paper_service


_event_redis = None


def _notify_local(user_id: str) -> None:
    """Notify this process's own sockets of `user_id`. Safe from any thread (the hub lives on the loop)."""
    loop = _loop
    if loop is None or getattr(loop, "is_closed", lambda: True)():
        return
    try:
        loop.call_soon_threadsafe(rt_hub.notify_user, user_id)
    except RuntimeError:
        pass  # loop shutting down


def notify_paper_changed(user_id: str) -> None:
    """Tell every open socket of one user, on any gateway, that their paper book changed.

    Gateway mode goes through Redis so a user's other tabs on other gateways hear it too (this process
    receives its own message back, so it is not also notified directly). If Redis cannot be reached,
    or there is no Redis, this process's sockets are notified directly."""
    global _event_redis
    if rt_settings.is_gateway():
        try:
            if _event_redis is None:
                import redis as _redis

                from src.realtime.redis_bus import redis_url

                _event_redis = _redis.Redis.from_url(redis_url(), socket_timeout=0.5, socket_connect_timeout=0.5)
            publish_user_event(_event_redis, user_id)
            return
        except Exception:
            pass
    _notify_local(user_id)


def _notify_many_local(user_ids: set[str]) -> None:
    for user_id in user_ids:
        _notify_local(user_id)


def ensure_exit_worker() -> None:
    """Register the TP/SL exit worker with the engine. Only the process that owns the engine
    (embedded mode, or the ingestor) does this; a gateway never decides exits."""
    global _exit_worker
    if _exit_worker is None:
        _exit_worker = PaperExitWorker(on_exits=_notify_many_local)
        engine.add_chain_listener(_exit_worker)


def reset_paper_service() -> None:
    """Drop the cached service and unregister the exit worker (tests, shutdown)."""
    global _paper_service, _exit_worker
    if _exit_worker is not None:
        engine.remove_chain_listener(_exit_worker)
    _paper_service = _exit_worker = None


def control_client() -> ControlClient:
    global _control
    if _control is None:
        _control = ControlClient()
    return _control


async def engine_call(method: str, *args, **kwargs):
    """Call an engine method wherever the engine lives: directly (embedded) or via the
    leading ingestor (gateway). Raises ValueError for bad input in both cases."""
    if rt_settings.is_gateway():
        try:
            return await control_client().call(method, *args, **kwargs)
        except ControlRemoteError as exc:
            if exc.kind == "ValueError":
                raise ValueError(str(exc)) from exc
            raise
    return await asyncio.to_thread(getattr(engine, method), *args, **kwargs)


# Read-only engine answers many users ask for repeatedly. Short TTLs keep the load on the engine (or,
# in gateway mode, on the ingestor's control channel) independent of the number of viewers.
read_cache = ReadCache()
LIVE_TTL_SECONDS = 2.0  # data that is still changing (today, the live strike)
PAST_TTL_SECONDS = 60.0  # a finished trading session does not change
DATES_TTL_SECONDS = 10.0


def _session_ttl(date: str) -> float:
    return LIVE_TTL_SECONDS if date >= today_ist().isoformat() else PAST_TTL_SECONDS


async def cached_engine_call(ttl: float, method: str, *args):
    """`engine_call` through the read cache. The result is shared: never mutate it."""
    return await read_cache.get((method, args), ttl, lambda: engine_call(method, *args))


def _ensure_engine_running() -> None:
    """Bind the broadcast callback to the running loop and start the tick thread.

    Idempotent: unsubscribing first guarantees `_broadcast` is registered exactly
    once even if this runs again (e.g. restarting a crashed worker), so clients
    never receive duplicate snapshots. No-op on serverless hosts, where ticks are
    driven per request instead of by a background thread.
    """
    global _loop
    if IS_SERVERLESS or rt_settings.is_gateway():
        return
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        pass
    engine.unsubscribe(_broadcast)
    engine.subscribe(_broadcast)
    engine.start()


def _check_schema() -> None:
    from src.db.migrate import schema_status

    at_head, detail = schema_status()
    if not at_head:
        raise RuntimeError(f"Database schema is not up to date ({detail}). Run: alembic upgrade head")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _loop, _redis_subscriber, _control
    auth_settings.validate_startup_config()
    gateway = rt_settings.is_gateway()
    if gateway and not auth_settings.auth_required():
        raise RuntimeError("Refusing to start: REALTIME_MODE=gateway requires AUTH_MODE=required.")
    if auth_settings.auth_required():
        _check_schema()
        get_paper_service()
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        pass
    if gateway:
        # Market data arrives from Redis; this process never ticks and never decides exits.
        _redis_subscriber = RedisSubscriber(rt_hub)
        _redis_subscriber.start()
    else:
        if auth_settings.auth_required():
            ensure_exit_worker()
        engine.unsubscribe(_broadcast)
        engine.subscribe(_broadcast)
        # Only start the background tick thread in persistent-server mode (not serverless).
        if not IS_SERVERLESS:
            engine.start()
    try:
        yield
    finally:
        if gateway:
            if _redis_subscriber is not None:
                await _redis_subscriber.stop()
                _redis_subscriber = None
            if _control is not None:
                await _control.close()
                _control = None
        else:
            engine.unsubscribe(_broadcast)
        await rt_hub.close_all()
        reset_paper_service()
        engine.close()


app = FastAPI(title="Live Market Data", version="1.0.0", lifespan=lifespan)
app.include_router(auth_router)

_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,  # explicit allow-list; never "*" with credentials
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Requested-With"],
    )


# History and straddle responses can be hundreds of KB of repetitive JSON; WebSocket traffic is unaffected.
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.exception_handler(AuthError)
async def _auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
    headers: dict[str, str] = {}
    if exc.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if exc.retry_after:
        headers["Retry-After"] = str(int(exc.retry_after))
    return JSONResponse(
        status_code=exc.status,
        content={"detail": {"code": exc.code, "message": exc.message, **exc.extra}},
        headers=headers or None,
    )


@app.exception_handler(ControlUnavailable)
async def _control_unavailable_handler(_request: Request, exc: ControlUnavailable) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": {"code": "ingestor_unavailable", "message": str(exc)}},
        headers={"Retry-After": "2"},
    )


@app.exception_handler(ControlRemoteError)
async def _control_remote_error_handler(_request: Request, exc: ControlRemoteError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": {"code": "ingestor_error", "message": f"{exc.kind}: {exc}"}})


def _public_snapshot(snapshot: dict) -> dict:
    """The market snapshot shared with every client. With auth on, the global
    `paper_trading` block (one shared book) is omitted - each user reads their
    own book from /api/paper/state."""
    if auth_settings.auth_required() and "paper_trading" in snapshot:
        return {k: v for k, v in snapshot.items() if k != "paper_trading"}
    return snapshot


def _broadcast(snapshot: dict) -> None:
    try:
        _publisher.on_snapshot(snapshot)  # versioned topic updates for /ws/v1
    except Exception:  # a publishing bug must never stop the legacy stream or the tick thread
        rt_metrics.publishes.labels("error").inc()
    if _loop is None or not _ws_clients:
        return
    if getattr(_loop, "is_closed", lambda: True)():
        return
    payload = json.dumps(_public_snapshot(snapshot))

    async def _send_all() -> None:
        dead: list[WebSocket] = []
        for ws in list(_ws_clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _ws_clients.discard(ws)

    try:
        asyncio.run_coroutine_threadsafe(_send_all(), _loop)
    except Exception:
        pass


# -- Health ---------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict:
    """Liveness: the process is up. Says nothing about dependencies (see /api/ready)."""
    return {
        "status": "ok",
        "engine_running": engine.is_running,
        "clients": len(_ws_clients),
    }


@app.get("/api/ready")
def ready() -> JSONResponse:
    """Readiness: can this instance do its job right now?

    Feed staleness is reported but does not fail readiness: restarting every gateway because
    the ingestor is down would only turn a data outage into a full outage."""
    checks: dict[str, str] = {}
    gateway = rt_settings.is_gateway()
    if gateway:
        checks["redis"] = "ok" if _redis_subscriber is not None and _redis_subscriber.connected else "down"
    elif not IS_SERVERLESS:
        checks["engine"] = "ok" if engine.is_running else "down"
    if auth_settings.auth_required():
        try:
            from src.db.migrate import schema_status

            at_head, detail = schema_status()
            checks["database"] = "ok" if at_head else f"schema_outdated ({detail})"
        except Exception:
            checks["database"] = "unreachable"
    ok = all(v == "ok" for v in checks.values())
    body: dict = {"status": "ready" if ok else "not_ready", "checks": checks}
    if gateway:
        age = rt_hub.feed_age_ms()
        body["feed_age_ms"] = age
        body["feed"] = "none" if age is None else ("ok" if age < 15_000 else "stale")
    return JSONResponse(status_code=200 if ok else 503, content=body)


# -- Market data (shared, read-only) ---------------------------------------------


@app.get("/api/snapshot")
async def snapshot(_user: AuthUser = Depends(current_user)) -> dict:
    if rt_settings.is_gateway():  # rebuild the single-blob shape from the hub's topics
        return merge_topics({t: st.payload for t, st in rt_hub.states.items()}, engine.underlying)
    return _public_snapshot(engine.get_snapshot())


@app.get("/api/config")
async def config(_user: AuthUser = Depends(current_user)) -> dict:
    # Non-secret settings only — provider credentials stay server-side.
    cfg = load_config()
    return {
        "underlying": cfg.get("underlying", "NIFTY"),
        "data_source": cfg.get("data_source", "mock"),
        "market_data_provider": cfg.get("market_data_provider", "mock"),
        "greeks_source": cfg.get("greeks_source", "provider"),
        "execution_mode": cfg.get("execution_mode", "paper"),
        "poll_interval_seconds": cfg.get("poll_interval_seconds", 5),
        "candle_intervals": cfg.get("candle_intervals", [1, 5, 15]),
    }


@app.get("/api/history/candles")
async def history_candles(interval: int = 1, limit: int = 200, _user: AuthUser = Depends(current_user)) -> dict:
    """Completed candles read back from the persistence store (empty list if disabled)."""
    return {
        "interval_minutes": interval,
        "candles": await cached_engine_call(5.0, "get_persisted_candles", interval, min(max(limit, 1), 5000)),
    }


@app.get("/api/history/option-chain")
async def history_option_chain(limit: int = 200, _user: AuthUser = Depends(current_user)) -> dict:
    """Recent option chain/ATM snapshots read back from the persistence store."""
    return {"snapshots": await cached_engine_call(5.0, "get_persisted_option_chain_snapshots", min(max(limit, 1), 5000))}


@app.get("/api/history/dates")
async def history_dates(_user: AuthUser = Depends(current_user)) -> dict:
    """Every Asia/Kolkata trading date with persisted data (newest first),
    plus today's IST date — backs the Date/Session selector's Previous/Next/
    date-picker navigation."""
    return await cached_engine_call(DATES_TTL_SECONDS, "get_available_trading_dates")


@app.get("/api/history/session")
async def history_session(date: str, strike: float | None = None, user: AuthUser = Depends(current_user)) -> dict:
    """
    Read-only bundle for one past Asia/Kolkata trading date: underlying price
    summary, candles (every configured interval), option chain snapshots
    through the day, one strike's CE/PE/Straddle candles, and that day's
    paper trading history/P&L. `has_data: false` (not an error) if nothing
    was persisted for this date. `date` must be "YYYY-MM-DD".
    """
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must be in YYYY-MM-DD format.") from exc
    data = await cached_engine_call(_session_ttl(date), "get_historical_session", date, strike)
    if auth_settings.auth_required():
        # The engine's own paper section is global; replace it with this user's trades. `data` is
        # shared through the cache, so change a copy, never the original.
        trades = await asyncio.to_thread(get_paper_service().trades_for_date, user.id, date)
        data = {**data, "paper_trading": summarize_persisted_paper_trades(trades)}
    return data


@app.get("/api/straddle-candles")
async def straddle_candles(strike: float, date: str | None = None, _user: AuthUser = Depends(current_user)) -> dict:
    """CE/PE/Straddle OHLC candles (1m/5m/15m) for one strike. With no `date`
    (or `date` omitted), this is the live-tracked strike's in-memory series —
    the ATM Straddle Chart's normal data source. With `date` ("YYYY-MM-DD"),
    it instead reads that strike's persisted candles for a past Asia/Kolkata
    trading session (read-only, no live ticks). 404 if that strike has no
    data yet/for that date."""
    if date is not None:
        data = await cached_engine_call(_session_ttl(date), "get_persisted_strike_candles_for_date", strike, date)
        if data is None:
            raise HTTPException(status_code=404, detail=f"No candle data for strike {strike} on {date}")
        return data
    data = await cached_engine_call(LIVE_TTL_SECONDS, "get_strike_straddle_candles", strike)
    if data is None:
        raise HTTPException(status_code=404, detail=f"No candle data yet for strike {strike}")
    return data


@app.get("/api/pair-candles")
async def pair_candles(ce_strike: float, pe_strike: float, _user: AuthUser = Depends(current_user)) -> dict:
    """OHLC candles of CE@ce_strike + PE@pe_strike (a strangle; equal strikes give the straddle).

    The first request for a pair starts tracking it, so its series begins then (`tracked_since`);
    nothing earlier is reconstructed. 404 if either strike is not in the live chain."""
    if not (0 < ce_strike < 10_000_000 and 0 < pe_strike < 10_000_000):
        raise HTTPException(status_code=422, detail="strikes out of range")
    data = await cached_engine_call(LIVE_TTL_SECONDS, "get_pair_candles", ce_strike, pe_strike)
    if data is None:
        raise HTTPException(status_code=404, detail="Both strikes must be in the live option chain")
    return data


# -- Paper trading (per user when AUTH_MODE=required) ----------------------------


class OpenPositionRequest(BaseModel):
    option_type: str = Field(max_length=2)
    strike: float | None = Field(default=None, ge=0, le=10_000_000)
    quantity: int = Field(default=50, gt=0, le=1_000_000)
    side: str = Field(default="BUY", max_length=4)  # "BUY" (long) or "SELL" (short/write) — simulation only
    # Optional contract identity from the Option Chain row the user selected;
    # rejected if it no longer matches the current chain.
    expiry: str | None = Field(default=None, max_length=16)
    underlying: str | None = Field(default=None, max_length=32)


class PaperConfigRequest(BaseModel):
    take_profit_percent: float | None = Field(default=None, le=100_000)
    stop_loss_percent: float | None = Field(default=None, le=100_000)
    # ₹ (absolute P&L) Profit Target / Loss Limit — additional, optional
    # triggers alongside the percent ones. Omit/null to leave disabled.
    take_profit_amount: float | None = Field(default=None, le=1e12)
    stop_loss_amount: float | None = Field(default=None, le=1e12)


class ATMConfigRequest(BaseModel):
    atm_method: str | None = Field(default=None, max_length=16)
    delta_threshold: float | None = None


class CandleBasisRequest(BaseModel):
    candle_price_basis: str = Field(max_length=32)


class DataSourceRequest(BaseModel):
    data_source: str = Field(max_length=32)
    sequence: str | None = Field(default=None, max_length=64)
    speed: float | None = Field(default=None, gt=0, le=1000)


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@app.get("/api/paper/state")
def paper_state(user: AuthUser = Depends(current_user)) -> dict:
    """Current simulated positions, trade history, and TP/SL config."""
    if not auth_settings.auth_required():
        return engine.get_paper_state()
    return get_paper_service().state(user.id)


@app.post("/api/paper/open")
def paper_open(
    req: OpenPositionRequest,
    user: AuthUser = Depends(current_user),
    idempotency_key: str | None = Header(default=None, max_length=64),
) -> dict:
    """Open a SIMULATED CE/PE position. Never places a real broker order.

    Send an `Idempotency-Key` header to make retries safe: the same key returns
    the original response instead of opening a second position."""
    try:
        if not auth_settings.auth_required():
            position = engine.open_paper_position(
                req.option_type, req.strike, req.quantity, req.side, expiry=req.expiry, underlying=req.underlying
            )
            result = position.to_dict()
        else:
            result = get_paper_service().open_position(
                user.id,
                req.option_type,
                req.strike,
                req.quantity,
                req.side,
                expiry=req.expiry,
                underlying=req.underlying,
                idempotency_key=idempotency_key,
            )
    except (ValueError, PaperError) as exc:
        raise _bad_request(exc) from exc
    notify_paper_changed(user.id)  # the user's other tabs refetch at once
    return result


@app.post("/api/paper/close/{position_id}")
def paper_close(position_id: str, user: AuthUser = Depends(current_user)) -> dict:
    """Manually close a SIMULATED position at the current premium."""
    if not auth_settings.auth_required():
        position = engine.close_paper_position(position_id)
        result = position.to_dict() if position is not None else None
    else:
        result = get_paper_service().close_position(user.id, position_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Position not found or already closed.")
    notify_paper_changed(user.id)
    return result


@app.post("/api/paper/config")
def paper_config(req: PaperConfigRequest, user: AuthUser = Depends(current_user)) -> dict:
    """Update the simulated Take Profit / Stop Loss thresholds."""
    try:
        if not auth_settings.auth_required():
            result = engine.set_paper_config(
                req.take_profit_percent, req.stop_loss_percent, req.take_profit_amount, req.stop_loss_amount
            ).as_dict()
        else:
            result = get_paper_service().set_config(
                user.id, req.take_profit_percent, req.stop_loss_percent, req.take_profit_amount, req.stop_loss_amount
            )
    except (ValueError, PaperError) as exc:
        raise _bad_request(exc) from exc
    notify_paper_changed(user.id)
    return result


# -- Operator surface (global; admin only when AUTH_MODE=required) ----------------


@app.get("/api/logs")
async def logs(limit: int = 200, level: str | None = None, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Recent structured log entries (in-memory ring buffer), newest first."""
    return {"logs": await engine_call("get_recent_logs", min(max(limit, 1), 1000), level)}


@app.get("/api/settings")
async def get_settings(_user: AuthUser = Depends(current_user)) -> dict:
    """Current runtime-adjustable settings (data source, ATM method, TP/SL, etc.)."""
    return await engine_call("get_settings")


@app.post("/api/settings/atm")
async def set_atm_settings(req: ATMConfigRequest, _admin: AuthUser = Depends(require_admin)) -> dict:
    try:
        return await engine_call("set_atm_config", req.atm_method, req.delta_threshold)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@app.post("/api/settings/candle-basis")
async def set_candle_basis(req: CandleBasisRequest, _admin: AuthUser = Depends(require_admin)) -> dict:
    try:
        return await engine_call("set_candle_price_basis", req.candle_price_basis)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@app.post("/api/settings/data-source")
async def set_data_source(req: DataSourceRequest, _admin: AuthUser = Depends(require_admin)) -> dict:
    """
    Switch the live feed (e.g. into deterministic MOCK Replay/Test Mode).
    Never touches real broker orders; DHAN stays available as the future
    live provider behind the same pipeline.
    """
    try:
        return await engine_call("reconfigure_source", req.data_source, req.sequence, req.speed)
    except ValueError as exc:
        raise _bad_request(exc) from exc


# -- Live stream ----------------------------------------------------------------


async def _close_ws(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        pass


def _token_claims(token: object) -> tuple[str, float]:
    """(user id, expiry as epoch seconds) of a valid access token; AuthError otherwise."""
    if not isinstance(token, str):
        raise AuthError("not_authenticated", "Expected an auth frame.")
    claims = decode_access_token(token)
    return claims["sub"], float(claims["exp"])


async def _ws_authenticate(websocket: WebSocket) -> tuple[str, float] | None:
    """Read the first frame, expect {"type":"auth","token":...}, return (user id, token expiry).
    Closes the socket (4401) and returns None on any failure. Nothing is ever sent
    to an unauthenticated socket, and tokens are never accepted in the URL."""
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_SECONDS)
        if len(raw) > WS_MAX_MESSAGE_BYTES:
            raise AuthError("invalid_token", "Auth frame too large.")
        frame = json.loads(raw)
        if not isinstance(frame, dict) or frame.get("type") != "auth":
            raise AuthError("not_authenticated", "Expected an auth frame.")
        return _token_claims(frame.get("token"))
    except (asyncio.TimeoutError, json.JSONDecodeError, AuthError, WebSocketDisconnect):
        await _close_ws(websocket, rt_protocol.CLOSE_AUTH, "authentication failed")
        return None


class _TokenBucket:
    """Per-connection inbound rate limit (a client has no reason to send many frames)."""

    def __init__(self, rate: float = 10.0, burst: float = 30.0):
        self.rate, self.burst, self.tokens, self.stamp = rate, burst, burst, time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


@app.websocket("/ws/v1")
async def websocket_v1(websocket: WebSocket) -> None:
    """Topic-based live feed (see src/realtime/protocol.py).

    The client subscribes to exactly the topics its page shows and receives a snapshot per
    topic, then patches. Auth is the same first-frame handshake as /ws."""
    await websocket.accept()
    user_id: str | None = None
    token_expiry: float | None = None
    if auth_settings.auth_required():
        authenticated = await _ws_authenticate(websocket)
        if authenticated is None:
            return
        user_id, token_expiry = authenticated
    rt_hub.max_clients = auth_settings.max_ws_connections_total()
    rt_hub.max_clients_per_user = auth_settings.max_ws_connections_per_user()
    rejected = rt_hub.admit(user_id)
    if rejected is not None:
        rt_metrics.clients_dropped.labels(rejected).inc()  # server_at_capacity | too_many_connections: visible to alerting
        await _close_ws(websocket, rt_protocol.CLOSE_CAPACITY, rejected)
        return

    async def close(code: int, reason: str) -> None:
        await _close_ws(websocket, code, reason)

    session = rt_hub.add_client(websocket.send_text, close, user_id)
    session.queue_control(
        rt_protocol.control_message("hello", heartbeat_s=hub_module.HEARTBEAT_SECONDS, max_topics=hub_module.MAX_TOPICS_PER_CLIENT)
    )
    loop = asyncio.get_running_loop()
    expiry_timer: asyncio.TimerHandle | None = None

    def arm_expiry(expires_at: float) -> None:
        """Close this socket the moment its access token expires, unless it is renewed."""
        nonlocal expiry_timer
        if expiry_timer is not None:
            expiry_timer.cancel()
        expiry_timer = loop.call_later(
            max(0.0, expires_at - time.time()), session.kill, "token_expired", rt_protocol.CLOSE_AUTH
        )

    if token_expiry is not None:
        arm_expiry(token_expiry)
    bucket, strikes = _TokenBucket(), 0
    try:
        while not session.closed:
            raw = await websocket.receive_text()
            if len(raw) > WS_MAX_MESSAGE_BYTES:
                rt_metrics.inbound_rejected.labels("too_large").inc()
                session.kill("message_too_large", code=1009)
                return
            if not bucket.allow():
                strikes += 1
                rt_metrics.inbound_rejected.labels("rate_limited").inc()
                session.queue_control(rt_protocol.control_message("rate_limited", retry_after_s=1))
                if strikes > 50:
                    session.kill("rate_limit_abuse", code=rt_protocol.CLOSE_PROTOCOL)
                    return
                continue
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                session.queue_control(rt_protocol.error_message("bad_json", "Frame is not valid JSON."))
                continue
            if isinstance(frame, dict) and frame.get("type") == "auth":
                # In-band renewal: a fresh token for the SAME user replaces the expiring one.
                if token_expiry is None:  # auth is off, so there is nothing to renew
                    session.queue_control(rt_protocol.control_message("auth_ok", exp=None))
                    continue
                try:
                    new_user, token_expiry = _token_claims(frame.get("token"))
                    if new_user != user_id:
                        raise AuthError("invalid_token", "Token belongs to a different user.")
                except AuthError:
                    session.kill("auth_renewal_failed", code=rt_protocol.CLOSE_AUTH)
                    return
                arm_expiry(token_expiry)
                session.queue_control(rt_protocol.control_message("auth_ok", exp=int(token_expiry * 1000)))
                continue
            _handle_client_frame(session, frame)
    except WebSocketDisconnect:
        pass
    finally:
        if expiry_timer is not None:
            expiry_timer.cancel()
        await session.shutdown()


def _handle_client_frame(session, frame: object) -> None:
    if not isinstance(frame, dict):
        session.queue_control(rt_protocol.error_message("bad_frame", "Frame must be a JSON object."))
        return
    kind = frame.get("type")
    if kind in ("subscribe", "unsubscribe"):
        valid, errors = validate_topics(frame.get("topics"))
        for topic, message in errors:
            session.queue_control(rt_protocol.error_message("bad_topic", message, topic or None))
        if kind == "subscribe":
            if frame.get("batch") is True:
                session.batch = True  # this client unpacks `batch` frames; others never receive them
            done = session.subscribe(valid)
            session.queue_control(rt_protocol.control_message("subscribed", topics=done))
        else:
            done = session.unsubscribe(valid)
            session.queue_control(rt_protocol.control_message("unsubscribed", topics=done))
    elif kind == "resync":
        topic = frame.get("topic")
        if not isinstance(topic, str) or not session.request_resync(topic):
            session.queue_control(rt_protocol.error_message("not_subscribed", "Subscribe before requesting a resync.", str(topic)[:40]))
    elif kind == "ping":
        session.queue_control(rt_protocol.control_message("pong", t=int(time.time() * 1000)))
    else:
        session.queue_control(rt_protocol.error_message("unknown_type", "Unknown frame type."))


@app.get("/metrics")
def metrics_endpoint(request: Request) -> Response:
    """Prometheus metrics. With METRICS_TOKEN set, requires `Authorization: Bearer <token>`.
    Without it the endpoint is open only when AUTH_MODE=disabled (local development) and
    does not exist otherwise: operational metrics must not be world-readable."""
    expected = os.getenv("METRICS_TOKEN")
    if expected:
        supplied = request.headers.get("authorization", "").partition(" ")[2]
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="Invalid metrics token.", headers={"WWW-Authenticate": "Bearer"})
    elif auth_settings.auth_required():
        raise HTTPException(status_code=404, detail="Not found")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Live snapshots.

    AUTH_MODE=disabled: connect and receive immediately (legacy behaviour).
    AUTH_MODE=required: the first client frame must be {"type":"auth","token":"<access token>"}
    within WS_AUTH_TIMEOUT_SECONDS. Tokens are never accepted in the URL, where proxies
    and access logs would record them. Nothing is sent before authentication succeeds.
    """
    await websocket.accept()
    if rt_settings.is_gateway():
        await _close_ws(websocket, 1008, "use /ws/v1")  # the single-blob stream exists only where the engine runs
        return
    user_id: str | None = None
    if auth_settings.auth_required():
        authenticated = await _ws_authenticate(websocket)
        if authenticated is None:
            return
        user_id = authenticated[0]  # the legacy /ws predates token expiry handling; /ws/v1 enforces it
        if len(_ws_clients) >= auth_settings.max_ws_connections_total():
            await _close_ws(websocket, 4429, "server at capacity")
            return
        if _ws_per_user[user_id] >= auth_settings.max_ws_connections_per_user():
            await _close_ws(websocket, 4429, "too many connections for this account")
            return
        _ws_per_user[user_id] += 1
    _ws_clients.add(websocket)
    try:
        await websocket.send_text(json.dumps(_public_snapshot(engine.get_snapshot())))
        while True:
            message = await websocket.receive_text()
            if len(message) > WS_MAX_MESSAGE_BYTES:
                await _close_ws(websocket, 1009, "message too large")
                return
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)
        if user_id is not None:
            _ws_per_user[user_id] -= 1
            if _ws_per_user[user_id] <= 0:
                del _ws_per_user[user_id]


# In Vercel serverless environment, frontend static files are served directly from
# the project root 'public/' directory by the Vercel CDN, eliminating the need for
# FastAPI static file collection and preventing static collection warnings.
# For standalone local development, FastAPI mounts static files and serves index.html.
if not IS_SERVERLESS:
    frontend_dir = ROOT / "public" if (ROOT / "public").exists() else ROOT / "frontend"
    if frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

    _spa_dist = spa_dist(ROOT)
    if _spa_dist is not None:
        # The React app (web/dist) owns "/" and every client-side route; "/static" above still
        # serves the legacy assets, and FRONTEND=legacy restores the old UI without a rebuild.
        install_spa(app, _spa_dist)
    else:

        @app.get("/")
        async def index() -> FileResponse:
            index_file = ROOT / "public" / "index.html" if (ROOT / "public" / "index.html").exists() else ROOT / "frontend" / "index.html"
            return FileResponse(index_file)


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    """Reject oversized request bodies up front (Content-Length based; a reverse
    proxy should enforce the same limit for chunked uploads)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"detail": "Request body too large."})
    return await call_next(request)


@app.middleware("http")
async def security_headers_and_frontend_cache(request: Request, call_next):
    """
    Baseline security headers on every response, plus cache control for the
    frontend.

    Neither StaticFiles nor FileResponse sets Cache-Control, so browsers fall
    back to heuristic freshness (commonly ~10% of file age) for "/" and
    "/static/*". During active local development that's a real trap: after
    editing index.html/app.js/styles.css, a plain reload can still serve the
    pre-edit copy from the browser's disk cache with no request ever hitting
    this server (so no log line, no clue) — only a hard refresh (Ctrl+Shift+R)
    forces revalidation. Force revalidation on every load instead: keep the
    existing ETag/Last-Modified (still enables a cheap 304) but require the
    browser to ask every time rather than trusting a heuristic window.
    """
    if not IS_SERVERLESS and not rt_settings.is_gateway() and not engine.is_running:
        _ensure_engine_running()
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if auth_settings.cookie_secure():
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    elif request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"  # content-hashed file names
    elif request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response
