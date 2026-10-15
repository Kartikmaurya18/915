from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.api.auth_routes import router as auth_router
from src.auth import settings as auth_settings
from src.auth.deps import AuthUser, authenticate_token, current_user, require_admin
from src.auth.security import AuthError
from src.config import load_config
from src.engine import MarketEngine
from src.paper.service import PaperError, UserPaperService
from src.serializers import summarize_persisted_paper_trades

ROOT = Path(__file__).resolve().parent.parent.parent
IS_SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", "65536"))
WS_AUTH_TIMEOUT_SECONDS = float(os.getenv("WS_AUTH_TIMEOUT_SECONDS", "5"))
WS_MAX_MESSAGE_BYTES = 4096  # clients only ever send a tiny auth/ping frame

engine = MarketEngine(load_config())
_ws_clients: set[WebSocket] = set()
_ws_per_user: Counter[str] = Counter()
_loop: asyncio.AbstractEventLoop | None = None
_paper_service: UserPaperService | None = None


def get_paper_service() -> UserPaperService:
    """The per-user paper service (AUTH_MODE=required only), started on first use."""
    global _paper_service
    if _paper_service is None:
        service = UserPaperService(engine)
        service.start()
        _paper_service = service
    return _paper_service


def reset_paper_service() -> None:
    """Drop the cached service (tests, or after the database is swapped)."""
    global _paper_service
    if _paper_service is not None:
        _paper_service.stop()
    _paper_service = None


def _ensure_engine_running() -> None:
    """Bind the broadcast callback to the running loop and start the tick thread.

    Idempotent: unsubscribing first guarantees `_broadcast` is registered exactly
    once even if this runs again (e.g. restarting a crashed worker), so clients
    never receive duplicate snapshots. No-op on serverless hosts, where ticks are
    driven per request instead of by a background thread.
    """
    global _loop
    if IS_SERVERLESS:
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
    global _loop
    auth_settings.validate_startup_config()
    if auth_settings.auth_required():
        _check_schema()
        get_paper_service()
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        pass
    engine.unsubscribe(_broadcast)
    engine.subscribe(_broadcast)
    # Only start the background tick thread in persistent-server mode (not serverless).
    if not IS_SERVERLESS:
        engine.start()
    try:
        yield
    finally:
        engine.unsubscribe(_broadcast)
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


@app.exception_handler(AuthError)
async def _auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"detail": {"code": exc.code, "message": exc.message}},
        headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None,
    )


def _public_snapshot(snapshot: dict) -> dict:
    """The market snapshot shared with every client. With auth on, the global
    `paper_trading` block (one shared book) is omitted - each user reads their
    own book from /api/paper/state."""
    if auth_settings.auth_required() and "paper_trading" in snapshot:
        return {k: v for k, v in snapshot.items() if k != "paper_trading"}
    return snapshot


def _broadcast(snapshot: dict) -> None:
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
    """Readiness: can this instance do its job right now?"""
    checks: dict[str, str] = {}
    if not IS_SERVERLESS:
        checks["engine"] = "ok" if engine.is_running else "down"
    if auth_settings.auth_required():
        try:
            from src.db.migrate import schema_status

            at_head, detail = schema_status()
            checks["database"] = "ok" if at_head else f"schema_outdated ({detail})"
        except Exception:
            checks["database"] = "unreachable"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"status": "ready" if ok else "not_ready", "checks": checks})


# -- Market data (shared, read-only) ---------------------------------------------


@app.get("/api/snapshot")
async def snapshot(_user: AuthUser = Depends(current_user)) -> dict:
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
        "candles": engine.get_persisted_candles(interval, min(max(limit, 1), 5000)),
    }


@app.get("/api/history/option-chain")
async def history_option_chain(limit: int = 200, _user: AuthUser = Depends(current_user)) -> dict:
    """Recent option chain/ATM snapshots read back from the persistence store."""
    return {"snapshots": engine.get_persisted_option_chain_snapshots(min(max(limit, 1), 5000))}


@app.get("/api/history/dates")
async def history_dates(_user: AuthUser = Depends(current_user)) -> dict:
    """Every Asia/Kolkata trading date with persisted data (newest first),
    plus today's IST date — backs the Date/Session selector's Previous/Next/
    date-picker navigation."""
    return engine.get_available_trading_dates()


@app.get("/api/history/session")
def history_session(date: str, strike: float | None = None, user: AuthUser = Depends(current_user)) -> dict:
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
    data = engine.get_historical_session(date, strike)
    if auth_settings.auth_required():
        # The engine's own paper section is global; replace it with this user's trades.
        trades = get_paper_service().trades_for_date(user.id, date)
        data["paper_trading"] = summarize_persisted_paper_trades(trades)
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
        data = engine.get_persisted_strike_candles_for_date(strike, date)
        if data is None:
            raise HTTPException(status_code=404, detail=f"No candle data for strike {strike} on {date}")
        return data
    data = engine.get_strike_straddle_candles(strike)
    if data is None:
        raise HTTPException(status_code=404, detail=f"No candle data yet for strike {strike}")
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
            return position.to_dict()
        return get_paper_service().open_position(
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
    return result


@app.post("/api/paper/config")
def paper_config(req: PaperConfigRequest, user: AuthUser = Depends(current_user)) -> dict:
    """Update the simulated Take Profit / Stop Loss thresholds."""
    try:
        if not auth_settings.auth_required():
            return engine.set_paper_config(
                req.take_profit_percent, req.stop_loss_percent, req.take_profit_amount, req.stop_loss_amount
            ).as_dict()
        return get_paper_service().set_config(
            user.id, req.take_profit_percent, req.stop_loss_percent, req.take_profit_amount, req.stop_loss_amount
        )
    except (ValueError, PaperError) as exc:
        raise _bad_request(exc) from exc


# -- Operator surface (global; admin only when AUTH_MODE=required) ----------------


@app.get("/api/logs")
async def logs(limit: int = 200, level: str | None = None, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Recent structured log entries (in-memory ring buffer), newest first."""
    return {"logs": engine.get_recent_logs(min(max(limit, 1), 1000), level)}


@app.get("/api/settings")
async def get_settings(_user: AuthUser = Depends(current_user)) -> dict:
    """Current runtime-adjustable settings (data source, ATM method, TP/SL, etc.)."""
    return engine.get_settings()


@app.post("/api/settings/atm")
async def set_atm_settings(req: ATMConfigRequest, _admin: AuthUser = Depends(require_admin)) -> dict:
    try:
        return engine.set_atm_config(req.atm_method, req.delta_threshold)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@app.post("/api/settings/candle-basis")
async def set_candle_basis(req: CandleBasisRequest, _admin: AuthUser = Depends(require_admin)) -> dict:
    try:
        return engine.set_candle_price_basis(req.candle_price_basis)
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
        return engine.reconfigure_source(req.data_source, req.sequence, req.speed)
    except ValueError as exc:
        raise _bad_request(exc) from exc


# -- Live stream ----------------------------------------------------------------


async def _close_ws(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        pass


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Live snapshots.

    AUTH_MODE=disabled: connect and receive immediately (legacy behaviour).
    AUTH_MODE=required: the first client frame must be {"type":"auth","token":"<access token>"}
    within WS_AUTH_TIMEOUT_SECONDS. Tokens are never accepted in the URL, where proxies
    and access logs would record them. Nothing is sent before authentication succeeds.
    """
    await websocket.accept()
    user_id: str | None = None
    if auth_settings.auth_required():
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_SECONDS)
            if len(raw) > WS_MAX_MESSAGE_BYTES:
                raise AuthError("invalid_token", "Auth frame too large.")
            frame = json.loads(raw)
            if not isinstance(frame, dict) or frame.get("type") != "auth" or not isinstance(frame.get("token"), str):
                raise AuthError("not_authenticated", "Expected an auth frame.")
            user_id = authenticate_token(frame["token"]).id
        except (asyncio.TimeoutError, json.JSONDecodeError, AuthError, WebSocketDisconnect):
            await _close_ws(websocket, 4401, "authentication failed")
            return
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
    if not IS_SERVERLESS and not engine.is_running:
        _ensure_engine_running()
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if auth_settings.cookie_secure():
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    elif request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response
