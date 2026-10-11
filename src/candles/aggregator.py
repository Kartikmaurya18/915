"""
Provider-independent OHLC candle engine: consumes normalized PriceTicks
(from MOCK today, Dhan later — it never knows which) and buckets them on
the Asia/Kolkata session grid (see src/timeutil.py).
"""

from datetime import datetime, timedelta, tzinfo

from src.models import Candle, PriceTick
from src.timeutil import get_zone


def _get_tz(tz_input: str | tzinfo | None) -> tzinfo | None:
    return None if tz_input is None else get_zone(tz_input)


def _parse_session_minute(session_start: str | None) -> int:
    if not session_start:
        return 0
    try:
        parts = session_start.split(":")
        return int(parts[1])
    except Exception:
        return 0


def _floor_to_interval(
    ts: datetime,
    interval_minutes: int,
    timezone: str | tzinfo | None = None,
    session_start: str | None = "09:15",
) -> datetime:
    """Align timestamp to the start of its candle bucket in the configured timezone and market session."""
    tz = _get_tz(timezone)
    if tz is not None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=tz)
        else:
            ts = ts.astimezone(tz)

    anchor_minute = _parse_session_minute(session_start)
    minute = ts.minute
    offset = (minute - anchor_minute) % interval_minutes
    floored_minute = minute - offset

    extra_hours = 0
    if floored_minute < 0:
        extra_hours = floored_minute // 60
        floored_minute = floored_minute % 60

    result = ts.replace(minute=floored_minute, second=0, microsecond=0)
    if extra_hours != 0:
        result += timedelta(hours=extra_hours)
    return result


class CandleAggregator:
    """
    Converts a stream of price ticks into OHLC candles at multiple intervals.
    Candles build in real time and finalize when the interval boundary is crossed.
    """

    def __init__(
        self,
        symbol: str,
        intervals_minutes: list[int],
        timezone: str | tzinfo | None = "Asia/Kolkata",
        session_start: str | None = "09:15",
    ):
        self.symbol = symbol
        self.intervals = sorted(set(intervals_minutes))
        if not self.intervals or any(
            not isinstance(interval, int) or interval <= 0 for interval in self.intervals
        ):
            raise ValueError("intervals_minutes must contain positive integers.")
        self.timezone = timezone
        self.session_start = session_start
        self._active: dict[int, Candle] = {}
        self._completed: dict[int, list[Candle]] = {m: [] for m in self.intervals}

    @property
    def active_candles(self) -> dict[int, Candle]:
        return dict(self._active)

    @property
    def completed_candles(self) -> dict[int, list[Candle]]:
        return self._completed

    def process_tick(self, tick: PriceTick) -> list[Candle]:
        """Ingest one tick; return any candles that just completed."""
        finalized: list[Candle] = []

        for interval in self.intervals:
            bucket_start = _floor_to_interval(
                tick.timestamp, interval, timezone=self.timezone, session_start=self.session_start
            )
            bucket_end = bucket_start + timedelta(minutes=interval)
            current = self._active.get(interval)

            if current is None:
                self._active[interval] = self._new_candle(
                    tick, interval, bucket_start, bucket_end
                )
                continue

            if bucket_start > current.open_time:
                current.is_complete = True
                self._completed[interval].append(current)
                finalized.append(current)
                self._active[interval] = self._new_candle(
                    tick, interval, bucket_start, bucket_end
                )
            else:
                current.update(tick.price)

        return finalized

    def _new_candle(
        self,
        tick: PriceTick,
        interval: int,
        open_time: datetime,
        close_time: datetime,
    ) -> Candle:
        candle = Candle(
            symbol=self.symbol,
            interval_minutes=interval,
            open_time=open_time,
            close_time=close_time,
            open=tick.price,
            high=tick.price,
            low=tick.price,
            close=tick.price,
            tick_count=1,
        )
        return candle

    def snapshot_active(self) -> dict[int, Candle]:
        """Return a shallow copy of in-progress candles for display."""
        return dict(self._active)
