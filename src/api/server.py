from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.config import load_config
from src.engine import MarketEngine

ROOT = Path(__file__).resolve().parent.parent.parent
IS_SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))

app = FastAPI(title="Live Market Data", version="1.0.0")
engine = MarketEngine(load_config())
_ws_clients: set[WebSocket] = set()
_loop: asyncio.AbstractEventLoop | None = None


def _broadcast(snapshot: dict) -> None:
    if _loop is None or not _ws_clients:
        return
    if getattr(_loop, "is_closed", lambda: True)():
        return
    payload = json.dumps(snapshot)

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


@app.on_event("startup")
async def startup() -> None:
    global _loop
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        pass
    engine.subscribe(_broadcast)
    # Only start background tick thread in persistent server mode (not in Vercel serverless)
    if not (os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME")):
        engine.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    engine.unsubscribe(_broadcast)
    engine.close()


@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "engine_running": engine.is_running,
        "clients": len(_ws_clients),
    }


@app.get("/api/snapshot")
async def snapshot() -> dict:
    return engine.get_snapshot()


@app.get("/api/config")
async def config() -> dict:
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
async def history_candles(interval: int = 1, limit: int = 200) -> dict:
    """Completed candles read back from the persistence store (empty list if disabled)."""
    return {
        "interval_minutes": interval,
        "candles": engine.get_persisted_candles(interval, limit),
    }


@app.get("/api/history/option-chain")
async def history_option_chain(limit: int = 200) -> dict:
    """Recent option chain/ATM snapshots read back from the persistence store."""
    return {"snapshots": engine.get_persisted_option_chain_snapshots(limit)}


@app.get("/api/history/dates")
async def history_dates() -> dict:
    """Every Asia/Kolkata trading date with persisted data (newest first),
    plus today's IST date — backs the Date/Session selector's Previous/Next/
    date-picker navigation."""
    return engine.get_available_trading_dates()


@app.get("/api/history/session")
async def history_session(date: str, strike: float | None = None) -> dict:
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
    return engine.get_historical_session(date, strike)


@app.get("/api/straddle-candles")
async def straddle_candles(strike: float, date: str | None = None) -> dict:
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


class OpenPositionRequest(BaseModel):
    option_type: str
    strike: float | None = None
    quantity: int = 50
    side: str = "BUY"  # "BUY" (long) or "SELL" (short/write) — simulation only
    # Optional contract identity from the Option Chain row the user selected;
    # rejected if it no longer matches the current chain.
    expiry: str | None = None
    underlying: str | None = None


class PaperConfigRequest(BaseModel):
    take_profit_percent: float | None = None
    stop_loss_percent: float | None = None
    # ₹ (absolute P&L) Profit Target / Loss Limit — additional, optional
    # triggers alongside the percent ones. Omit/null to leave disabled.
    take_profit_amount: float | None = None
    stop_loss_amount: float | None = None


class ATMConfigRequest(BaseModel):
    atm_method: str | None = None
    delta_threshold: float | None = None


class CandleBasisRequest(BaseModel):
    candle_price_basis: str


class DataSourceRequest(BaseModel):
    data_source: str
    sequence: str | None = None
    speed: float | None = None


@app.get("/api/paper/state")
async def paper_state() -> dict:
    """Current simulated positions, trade history, and TP/SL config."""
    return engine.get_paper_state()


@app.post("/api/paper/open")
async def paper_open(req: OpenPositionRequest) -> dict:
    """Open a SIMULATED CE/PE position. Never places a real broker order."""
    try:
        position = engine.open_paper_position(
            req.option_type, req.strike, req.quantity, req.side, expiry=req.expiry, underlying=req.underlying
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return position.to_dict()


@app.post("/api/paper/close/{position_id}")
async def paper_close(position_id: str) -> dict:
    """Manually close a SIMULATED position at the current premium."""
    position = engine.close_paper_position(position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="Position not found or already closed.")
    return position.to_dict()


@app.post("/api/paper/config")
async def paper_config(req: PaperConfigRequest) -> dict:
    """Update the simulated Take Profit / Stop Loss thresholds."""
    try:
        cfg = engine.set_paper_config(
            req.take_profit_percent,
            req.stop_loss_percent,
            req.take_profit_amount,
            req.stop_loss_amount,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return cfg.as_dict()


@app.get("/api/logs")
async def logs(limit: int = 200, level: str | None = None) -> dict:
    """Recent structured log entries (in-memory ring buffer), newest first."""
    return {"logs": engine.get_recent_logs(limit, level)}


@app.get("/api/settings")
async def get_settings() -> dict:
    """Current runtime-adjustable settings (data source, ATM method, TP/SL, etc.)."""
    return engine.get_settings()


@app.post("/api/settings/atm")
async def set_atm_settings(req: ATMConfigRequest) -> dict:
    try:
        return engine.set_atm_config(req.atm_method, req.delta_threshold)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings/candle-basis")
async def set_candle_basis(req: CandleBasisRequest) -> dict:
    try:
        return engine.set_candle_price_basis(req.candle_price_basis)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings/data-source")
async def set_data_source(req: DataSourceRequest) -> dict:
    """
    Switch the live feed (e.g. into deterministic MOCK Replay/Test Mode).
    Never touches real broker orders; DHAN stays available as the future
    live provider behind the same pipeline.
    """
    try:
        return engine.reconfigure_source(req.data_source, req.sequence, req.speed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        await websocket.send_text(json.dumps(engine.get_snapshot()))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)


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
async def no_cache_for_frontend(request, call_next):
    """
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
    global _loop
    if not IS_SERVERLESS:
        if not engine.is_running:
            try:
                _loop = asyncio.get_running_loop()
                engine.subscribe(_broadcast)
            except RuntimeError:
                pass
            engine.start()
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response
