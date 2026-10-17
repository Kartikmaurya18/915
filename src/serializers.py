from __future__ import annotations

from datetime import datetime

from src.candles import CandleAggregator
from src.models import ATMResult, Candle, OptionChainSnapshot, PriceTick
from src.timeutil import to_ist


def _dt(value: datetime | None) -> str | None:
    """Serialize a datetime to an IST-offset ISO string — the single point
    where every timestamp sent to the frontend is normalized to Asia/Kolkata,
    regardless of whether it arrived naive or aware, or from a server
    process running in a different timezone."""
    return to_ist(value).isoformat() if value else None


def _ts(value: datetime) -> int:
    return int(value.timestamp())


def serialize_candle(candle: Candle, complete: bool | None = None) -> dict:
    return {
        "symbol": candle.symbol,
        "interval_minutes": candle.interval_minutes,
        "open_time": _dt(candle.open_time),
        "close_time": _dt(candle.close_time),
        "time": _ts(candle.open_time),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "tick_count": candle.tick_count,
        "is_complete": complete if complete is not None else candle.is_complete,
    }


def serialize_atm(atm: ATMResult | None) -> dict | None:
    if atm is None:
        return None
    return {
        "strike": atm.strike,
        "call_ltp": atm.call_ltp,
        "put_ltp": atm.put_ltp,
        "straddle_premium": atm.straddle_premium,
        "combined_value": atm.straddle_premium,  # ATM CE + PE, alias for on-screen label
        "underlying_ltp": atm.underlying_ltp,
        "distance_from_spot": atm.distance_from_spot,
        "method": atm.method,
        "call_delta": atm.call_delta,
        "put_delta": atm.put_delta,
        "delta_threshold": atm.delta_threshold,
        "price_based_strike": atm.price_based_strike,
        "greeks_source": atm.greeks_source,
    }


def serialize_strike_candles(strike: float, builders: dict[str, CandleAggregator]) -> dict:
    """CE/PE/Straddle OHLC candles for one fixed strike, at every interval —
    backs the ATM Straddle Chart's candlestick view (GET /api/straddle-candles).
    Reuses the exact same serialize_candle() the main candlestick chart uses;
    each leg's CandleAggregator was fed that strike's own price only, so
    there is nothing here to guard against mixing strikes."""
    by_interval: dict[str, dict] = {}
    for interval in builders["straddle"].intervals:
        by_interval[str(interval)] = {}
        for leg in ("ce", "pe", "straddle"):
            agg = builders[leg]
            completed = agg.completed_candles.get(interval, [])
            active = agg.active_candles.get(interval)
            series = [serialize_candle(c) for c in completed[-150:]]
            if active is not None:
                series.append(serialize_candle(active, complete=False))
            by_interval[str(interval)][leg] = series
    return {"strike": strike, "by_interval": by_interval}


def _oi_change(current: int | None, previous: int | None) -> int | None:
    return current - previous if current is not None and previous is not None else None


def serialize_chain(chain: OptionChainSnapshot | None, atm_strike: float | None) -> dict | None:
    if chain is None:
        return None

    strikes = []
    for leg in chain.strikes:
        strikes.append(
            {
                "strike": leg.strike,
                "call_ltp": leg.call_ltp,
                "put_ltp": leg.put_ltp,
                "call_oi": leg.call_oi,
                "put_oi": leg.put_oi,
                "call_volume": leg.call_volume,
                "put_volume": leg.put_volume,
                "call_delta": leg.call_delta,
                "put_delta": leg.put_delta,
                # Optional normalized fields — None when the provider doesn't supply them.
                "call_iv": leg.call_iv,
                "put_iv": leg.put_iv,
                "call_bid": leg.call_bid,
                "call_ask": leg.call_ask,
                "put_bid": leg.put_bid,
                "put_ask": leg.put_ask,
                "call_gamma": leg.call_gamma,
                "put_gamma": leg.put_gamma,
                "call_theta": leg.call_theta,
                "put_theta": leg.put_theta,
                "call_vega": leg.call_vega,
                "put_vega": leg.put_vega,
                # Change since the previous session's close; None unless both numbers exist.
                "call_oi_change": _oi_change(leg.call_oi, leg.call_oi_prev),
                "put_oi_change": _oi_change(leg.put_oi, leg.put_oi_prev),
                "call_instrument_id": leg.call_instrument_id,
                "put_instrument_id": leg.put_instrument_id,
                # Same isolated CE+PE formula used everywhere else (MockPriceCalculator._combined_value)
                "combined_value": leg.call_ltp + leg.put_ltp,
                "is_atm": leg.strike == atm_strike if atm_strike is not None else False,
            }
        )

    return {
        "underlying": chain.underlying,
        "underlying_ltp": chain.underlying_ltp,
        "expiry": chain.expiry,
        "expiries": list(chain.expiries),
        "expiry_type": chain.expiry_type,  # Configurable expiry type
        "provider": chain.provider,
        "exchange": chain.exchange,
        "greeks_source": chain.greeks_source,
        "timestamp": _dt(chain.timestamp),
        "trading_date": chain.trading_date,
        "strikes": strikes,
    }


def serialize_persisted_candle(row: dict) -> dict:
    """Shape a raw SQLite `candles` row (see PersistenceStore) into the same
    dict shape serialize_candle() produces, so the frontend's existing chart/
    table rendering can be reused unchanged for historical data. Persisted
    rows are always completed candles."""
    open_time = datetime.fromisoformat(row["open_time"])
    return {
        "symbol": row["symbol"],
        "interval_minutes": row["interval_minutes"],
        "open_time": _dt(open_time),
        "close_time": _dt(datetime.fromisoformat(row["close_time"])),
        "time": _ts(open_time),
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "tick_count": row["tick_count"],
        "is_complete": True,
    }


def _empty_paper_stats() -> dict:
    return {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate_percent": 0.0,
        "total_pnl": 0.0,
        "average_win": 0.0,
        "average_loss": 0.0,
        "max_drawdown": 0.0,
    }


def _empty_pnl_summary() -> dict:
    return {
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "total_pnl": 0.0,
        "total_pnl_percent": 0.0,
        "buy_turnover": 0.0,
        "sell_turnover": 0.0,
        "total_turnover": 0.0,
        "total_position_value": 0.0,
        "open_position_count": 0,
    }


def serialize_persisted_paper_trade(row: dict) -> dict:
    """Shape a raw SQLite `paper_trades` row into the same dict shape
    PaperPosition.to_dict() produces (the subset the History table and toast
    logic actually read), so frontend rendering is reused unchanged."""
    entry_price = row["entry_price"]
    exit_price = row["exit_price"]
    quantity = row["quantity"]
    return {
        "id": row["id"],
        "option_type": row["option_type"],
        "side": row["side"],
        "strike": row["strike"],
        "quantity": quantity,
        "entry_price": round(entry_price, 2) if entry_price is not None else None,
        "exit_price": round(exit_price, 2) if exit_price is not None else None,
        "entry_time": _dt(datetime.fromisoformat(row["entry_time"])) if row["entry_time"] else None,
        "exit_time": _dt(datetime.fromisoformat(row["exit_time"])) if row["exit_time"] else None,
        "exit_reason": row["exit_reason"],
        "status": "EXITED",
        "pnl": round(row["pnl"], 2) if row["pnl"] is not None else 0.0,
        "pnl_percent": round(row["pnl_percent"], 2) if row["pnl_percent"] is not None else 0.0,
        "unrealized_pnl": None,
        "realized_pnl": round(row["pnl"], 2) if row["pnl"] is not None else 0.0,
        "take_profit_percent": row["take_profit_percent"],
        "stop_loss_percent": row["stop_loss_percent"],
        "take_profit_amount": row["take_profit_amount"],
        "stop_loss_amount": row["stop_loss_amount"],
        "simulated": True,
    }


def summarize_persisted_paper_trades(trades: list[dict]) -> dict:
    """The historical counterpart of PaperTradingEngine.to_state_dict():
    closed-trade stats + portfolio P&L/turnover, computed straight from
    persisted rows for one past trading date. There are no "open positions"
    in a past, read-only session — every persisted trade is already closed,
    so unrealized P&L and open position value are always zero here."""
    history = [serialize_persisted_paper_trade(row) for row in reversed(trades)]

    if not trades:
        return {
            "simulated": True,
            "open_positions": [],
            "history": history,
            "stats": _empty_paper_stats(),
            "pnl_summary": _empty_pnl_summary(),
        }

    pnls = [row["pnl"] or 0.0 for row in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_pnl = sum(pnls)

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    stats = {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_percent": round((len(wins) / len(trades)) * 100.0, 2) if trades else 0.0,
        "total_pnl": round(total_pnl, 2),
        "average_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "average_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "max_drawdown": round(max_drawdown, 2),
    }

    buy_turnover = sum(row["entry_price"] * row["quantity"] for row in trades if row["side"] == "BUY")
    sell_turnover = sum(row["entry_price"] * row["quantity"] for row in trades if row["side"] == "SELL")
    total_turnover = buy_turnover + sell_turnover

    pnl_summary = {
        "unrealized_pnl": 0.0,
        "realized_pnl": round(total_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_percent": round((total_pnl / total_turnover) * 100.0, 2) if total_turnover else 0.0,
        "buy_turnover": round(buy_turnover, 2),
        "sell_turnover": round(sell_turnover, 2),
        "total_turnover": round(total_turnover, 2),
        "total_position_value": 0.0,
        "open_position_count": 0,
    }

    return {
        "simulated": True,
        "open_positions": [],
        "history": history,
        "stats": stats,
        "pnl_summary": pnl_summary,
    }


def serialize_snapshot(
    *,
    underlying: str,
    tick: PriceTick | None,
    atm: ATMResult | None,
    chain: OptionChainSnapshot | None,
    calculated_prices: dict | None = None,
    rrg_snapshot = None,
    active_candles: dict[int, Candle],
    completed_candles: dict[int, list[Candle]],
    newly_completed: list[Candle],
    tick_count: int,
    intervals: list[int],
    data_source: str,
    status: str = "LIVE",
    paper_trading: dict | None = None,
    last_tick_age_seconds: float | None = None,
    is_stale: bool = False,
    stale_threshold_seconds: float | None = None,
    persistence_enabled: bool = False,
    duplicate_tick_count: int = 0,
    rejected_tick_count: int = 0,
    market_session: str | None = None,
    settings: dict | None = None,
    provider: dict | None = None,
) -> dict:
    atm_strike = atm.strike if atm else None
    candles_by_interval: dict[str, dict] = {}

    for interval in intervals:
        completed = completed_candles.get(interval, [])
        active = active_candles.get(interval)
        series = [serialize_candle(c) for c in completed[-100:]]
        if active is not None:
            series.append(serialize_candle(active, complete=False))
        candles_by_interval[str(interval)] = {
            "active": serialize_candle(active, complete=False) if active else None,
            "completed": [serialize_candle(c) for c in completed[-20:]],
            "series": series,
        }

    # Serialize RRG snapshot if available
    rrg_data = None
    if rrg_snapshot:
        rrg_data = rrg_snapshot.to_dict()

    return {
        "underlying": underlying,
        "data_source": data_source,
        "status": status,
        # Provider identity/state (name, LIVE/SIMULATED/ERROR/..., reason,
        # available expiries, Greeks source, execution mode). Never contains
        # credentials.
        "provider": provider or {"name": data_source, "status": status},
        "tick_count": tick_count,
        "price": tick.price if tick else None,
        "timestamp": _dt(tick.timestamp) if tick else None,
        "data_quality": {
            "source": data_source,
            "connection_status": status,
            "tick_count": tick_count,
            "last_tick_time": _dt(tick.timestamp) if tick else None,
            "last_tick_age_seconds": (
                round(last_tick_age_seconds, 2) if last_tick_age_seconds is not None else None
            ),
            "is_stale": is_stale,
            "stale_threshold_seconds": stale_threshold_seconds,
            "persistence_enabled": persistence_enabled,
            "duplicate_tick_count": duplicate_tick_count,
            "rejected_tick_count": rejected_tick_count,
            "market_session": market_session,
        },
        "atm": serialize_atm(atm),
        "option_chain": serialize_chain(chain, atm_strike),
        "calculated_prices": calculated_prices,
        "rrg": rrg_data,
        "intervals": intervals,
        "candles": candles_by_interval,
        "newly_completed": [serialize_candle(c) for c in newly_completed],
        "paper_trading": paper_trading
        or {
            "simulated": True,
            "config": {"take_profit_percent": 10.0, "stop_loss_percent": 5.0},
            "open_positions": [],
            "history": [],
            "stats": {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate_percent": 0.0,
                "total_pnl": 0.0,
                "average_win": 0.0,
                "average_loss": 0.0,
                "max_drawdown": 0.0,
            },
        },
        "settings": settings or {},
    }
