"""
Data-quality helpers: staleness detection for the live tick stream.

Kept isolated (and pure/testable) so the engine's snapshot-building code stays
thin. "Stale" means no tick has been received within `threshold_seconds`,
regardless of what the connection-status badge (LIVE / SIMULATED / OFFLINE)
says — a feed can report "connected" while silently no longer sending ticks.
"""

from __future__ import annotations

from datetime import datetime

from src.timeutil import now_ist, to_ist


def compute_staleness(
    last_tick_time: datetime | None,
    now: datetime | None = None,
    threshold_seconds: float = 10.0,
) -> tuple[float | None, bool]:
    """
    Return (age_seconds, is_stale) for the most recent tick.

    - `last_tick_time` is None (no tick received yet) -> (None, True): treated
      as stale since there is nothing confirming the feed is alive.
    - Handles naive/aware datetime mixing by matching `now`'s awareness to
      `last_tick_time`'s.
    """
    if last_tick_time is None:
        return None, True

    if now is None:
        now = now_ist()

    # Normalize awareness so subtraction never raises. Naive datetimes are
    # IST wall-clock time by this codebase's convention (see timeutil).
    if last_tick_time.tzinfo is not None and now.tzinfo is None:
        now = to_ist(now)
    elif last_tick_time.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)

    age_seconds = (now - last_tick_time).total_seconds()
    is_stale = age_seconds > threshold_seconds
    return age_seconds, is_stale


def default_stale_threshold_seconds(poll_interval_seconds: float) -> float:
    """Sensible default: at least 10s, or 3x the poll interval if that's larger."""
    return max(10.0, float(poll_interval_seconds) * 3.0)
