"""
Per-strike OHLC candles for CE / PE / Straddle (CE+PE), at any strike present
in the option chain — not just ATM. Backs the ATM Straddle Chart's switch to
"currently selected strike" behavior: pick any row in the Option Chain and
get that strike's own candle history, independent of every other strike.

Deliberately reuses `CandleAggregator` (src/candles/aggregator.py) as-is —
the exact same OHLC/session-bucketing engine that drives the main
candlestick chart — rather than reimplementing bucketing logic. Each strike
gets three independent CandleAggregator instances (CE, PE, Straddle); since
a strike's series is permanently pinned to that one strike by construction,
there is no cross-strike mixing to guard against (unlike the earlier
ATM-tracking design, a fixed-strike series never needs to "split").
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from src.candles import CandleAggregator
from src.models import Candle, OptionChainSnapshot, PriceTick

LEGS = ("ce", "pe", "straddle")


class StrikeStraddleCandleEngine:
    """Tracks CE/PE/Straddle OHLC candles for every strike seen in the chain."""

    def __init__(
        self,
        intervals_minutes: list[int],
        timezone: str | tzinfo | None = "Asia/Kolkata",
        session_start: str | None = "09:15",
    ):
        self.intervals = list(intervals_minutes)
        self.timezone = timezone
        self.session_start = session_start
        # strike -> {"ce": CandleAggregator, "pe": CandleAggregator, "straddle": CandleAggregator}
        self._builders: dict[float, dict[str, CandleAggregator]] = {}
        # (underlying, expiry) the series above belong to. Strike alone only
        # identifies a contract within one underlying+expiry, so when either
        # changes (expiry rollover, provider/underlying switch) the series
        # restart rather than splice two different contracts into one bar.
        self._scope: tuple[str, str] | None = None

    def _get_or_create(self, strike: float) -> dict[str, CandleAggregator]:
        builders = self._builders.get(strike)
        if builders is None:
            builders = {
                leg: CandleAggregator(
                    symbol=f"{strike}_{leg}",
                    intervals_minutes=self.intervals,
                    timezone=self.timezone,
                    session_start=self.session_start,
                )
                for leg in LEGS
            }
            self._builders[strike] = builders
        return builders

    def process(self, chain: OptionChainSnapshot | None, timestamp: datetime) -> list[Candle]:
        """Feed one tick's worth of chain legs into every strike's
        aggregators; return any CE/PE/Straddle candles that just completed
        (for the caller to persist — see MarketEngine._tick_worker)."""
        if chain is None:
            return []
        scope = (chain.underlying, chain.expiry)
        if self._scope is not None and scope != self._scope:
            self._builders.clear()
        self._scope = scope
        finalized: list[Candle] = []
        for leg in chain.strikes:
            builders = self._get_or_create(leg.strike)
            finalized += builders["ce"].process_tick(
                PriceTick(symbol="CE", price=leg.call_ltp, timestamp=timestamp)
            )
            finalized += builders["pe"].process_tick(
                PriceTick(symbol="PE", price=leg.put_ltp, timestamp=timestamp)
            )
            finalized += builders["straddle"].process_tick(
                PriceTick(symbol="STRADDLE", price=leg.call_ltp + leg.put_ltp, timestamp=timestamp)
            )
        return finalized

    def has_strike(self, strike: float) -> bool:
        return strike in self._builders

    def known_strikes(self) -> list[float]:
        return sorted(self._builders.keys())

    def get_strike_builders(self, strike: float) -> dict[str, CandleAggregator] | None:
        return self._builders.get(strike)
