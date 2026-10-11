"""Live Market Data, Option Chain & Candle Generation System."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.display import ConsoleDisplay
from src.engine import MarketEngine
from src.api.server import app


def run(cfg: dict | None = None) -> None:
    cfg = cfg or load_config()
    engine = MarketEngine(cfg)
    display = ConsoleDisplay()
    worker_error: Exception | None = None

    def on_update(snapshot: dict) -> None:
        nonlocal worker_error
        if not engine.is_running and engine._worker_error is not None:
            worker_error = engine._worker_error

        candles = snapshot.get("candles", {})
        active = {
            int(k): _dict_to_candle(v["active"])
            for k, v in candles.items()
            if v.get("active")
        }
        completed_raw = snapshot.get("newly_completed", [])
        completed = [_dict_to_candle(c) for c in completed_raw]

        display.update(
            price=snapshot.get("price") or 0,
            atm=_dict_to_atm(snapshot.get("atm")),
            active_candles=active,
            completed=completed,
        )
        for candle in completed:
            display.print_completed_candle(candle)

    engine.subscribe(on_update)
    engine.start()

    from rich.live import Live

    try:
        with Live(display.render(), refresh_per_second=2, console=display.console) as live:
            while engine.is_running:
                live.update(display.render())
                time.sleep(0.5)
                if engine._worker_error is not None:
                    worker_error = engine._worker_error
                    break
    except KeyboardInterrupt:
        display.console.print("\n[bold yellow]Stopped.[/]")
    finally:
        engine.stop()

    if worker_error is not None:
        raise RuntimeError("Market data worker stopped unexpectedly.") from worker_error


def _dict_to_atm(data: dict | None):
    if not data:
        return None
    from src.models import ATMResult

    return ATMResult(
        strike=data["strike"],
        call_ltp=data["call_ltp"],
        put_ltp=data["put_ltp"],
        straddle_premium=data["straddle_premium"],
        underlying_ltp=data["underlying_ltp"],
        distance_from_spot=data["distance_from_spot"],
        method=data.get("method", "price"),
        call_delta=data.get("call_delta"),
        put_delta=data.get("put_delta"),
        delta_threshold=data.get("delta_threshold"),
        price_based_strike=data.get("price_based_strike"),
    )


def _dict_to_candle(data: dict):
    from datetime import datetime

    from src.models import Candle

    return Candle(
        symbol=data.get("symbol", ""),
        interval_minutes=data["interval_minutes"],
        open_time=datetime.fromisoformat(data["open_time"]),
        close_time=datetime.fromisoformat(data["close_time"]),
        open=data["open"],
        high=data["high"],
        low=data["low"],
        close=data["close"],
        tick_count=data.get("tick_count", 0),
        is_complete=data.get("is_complete", False),
    )


def main():
    parser = argparse.ArgumentParser(
        description="Live market data, option chain ATM, and multi-interval candles."
    )
    parser.add_argument(
        "--source",
        choices=["mock", "nse", "dhan"],
        help="Override config data_source ('mock', 'nse', 'dhan')",
    )
    parser.add_argument(
        "--underlying",
        help="Override underlying symbol (e.g. NIFTY, BANKNIFTY)",
    )
    parser.add_argument(
        "--poll",
        type=float,
        help="Poll interval in seconds",
    )
    args = parser.parse_args()

    cfg = load_config()
    if args.source:
        cfg["data_source"] = args.source
    if args.underlying:
        cfg["underlying"] = args.underlying.upper()
    if args.poll is not None:
        if args.poll <= 0:
            parser.error("--poll must be greater than zero")
        cfg["poll_interval_seconds"] = args.poll

    run(cfg)


if __name__ == "__main__":
    main()
