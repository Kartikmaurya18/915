"""
Tick-level reliability helpers: timestamp validation, duplicate/out-of-order
detection, and market-session classification.

Kept isolated and pure so each rule is directly unit-testable and the engine's
ingestion loop stays a thin wrapper around them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time, tzinfo

from src.market_calendar import is_market_holiday
from src.models import PriceTick
from src.timeutil import get_zone


@dataclass
class TickValidationResult:
    is_valid: bool
    is_duplicate: bool = False
    is_out_of_order: bool = False
    reason: str | None = None


def validate_tick(
    tick: PriceTick | None, last_accepted_timestamp: datetime | None
) -> TickValidationResult:
    """
    Validate an incoming tick before it reaches the candle aggregator / ATM
    pipeline.

    Rejects:
      - a tick with no timestamp, or a non-positive price (basic sanity —
        does not reject negative/zero downstream, only at the ingestion gate)
      - an exact repeat of the last accepted timestamp (duplicate delivery,
        e.g. after a source reconnect)
      - a timestamp older than the last accepted one (out-of-order; would
        corrupt OHLC ordering if it reached the aggregator)

    A tick equal in time to the last one is a duplicate, not out-of-order —
    order is only violated by strictly-earlier timestamps.
    """
    if tick is None or tick.timestamp is None:
        return TickValidationResult(is_valid=False, reason="missing_timestamp")
    if tick.price is None or tick.price <= 0:
        return TickValidationResult(is_valid=False, reason="non_positive_price")

    if last_accepted_timestamp is not None:
        if tick.timestamp == last_accepted_timestamp:
            return TickValidationResult(
                is_valid=False, is_duplicate=True, reason="duplicate_timestamp"
            )
        if tick.timestamp < last_accepted_timestamp:
            return TickValidationResult(
                is_valid=False, is_out_of_order=True, reason="out_of_order_timestamp"
            )

    return TickValidationResult(is_valid=True)


def get_market_session_status(
    timestamp: datetime,
    session_start: str = "09:15",
    session_end: str = "15:30",
    timezone: str | tzinfo | None = "Asia/Kolkata",
) -> str:
    """
    Classify a timestamp relative to the configured market session.
    Returns one of: "WEEKEND", "HOLIDAY", "PRE_OPEN", "OPEN", "CLOSED".

    Informational only — MOCK data is not gated by this (ticks keep flowing
    outside session hours for dev/demo convenience); it exists so the UI and
    downstream consumers can display accurate session state.
    """
    tz = get_zone(timezone)
    ts = timestamp.astimezone(tz) if timestamp.tzinfo else timestamp.replace(tzinfo=tz)

    if ts.weekday() >= 5:  # Saturday=5, Sunday=6
        return "WEEKEND"
    if is_market_holiday(ts.date()):
        return "HOLIDAY"

    start_h, start_m = (int(x) for x in session_start.split(":"))
    end_h, end_m = (int(x) for x in session_end.split(":"))
    start_t = dt_time(start_h, start_m)
    end_t = dt_time(end_h, end_m)
    current_t = ts.time()

    if current_t < start_t:
        return "PRE_OPEN"
    if current_t > end_t:
        return "CLOSED"
    return "OPEN"
