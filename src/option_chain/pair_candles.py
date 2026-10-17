"""
OHLC candles for a strangle (or any CE + PE pair at two different strikes).

The high of a sum is not the sum of the highs, so a pair's candles cannot be derived from the two
legs' own candles: the summed premium has to be fed to a CandleAggregator tick by tick. Tracking every
possible pair would grow with the square of the chain, so pairs are registered on demand (the first time
someone asks for one), kept in a bounded least-recently-requested cache, and fed from then on. A pair
therefore has no history before it was first requested, and `tracked_since` says exactly when it began:
earlier minutes are never reconstructed or guessed.

Reuses `CandleAggregator`, the same bucketing engine behind every other chart.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, tzinfo

from src.candles import CandleAggregator
from src.models import Candle, OptionChainSnapshot, PriceTick

MAX_PAIRS = 32


@dataclass
class _Pair:
    ce_strike: float
    pe_strike: float
    tracked_since: datetime
    aggregator: CandleAggregator


class PairCandleEngine:
    def __init__(
        self,
        intervals_minutes: list[int],
        timezone: str | tzinfo | None = "Asia/Kolkata",
        session_start: str | None = "09:15",
        max_pairs: int = MAX_PAIRS,
    ):
        self.intervals = list(intervals_minutes)
        self.timezone = timezone
        self.session_start = session_start
        self.max_pairs = max_pairs
        self._pairs: OrderedDict[tuple[float, float], _Pair] = OrderedDict()
        # (underlying, expiry): strikes only identify contracts within one of these, so a change restarts every pair.
        self._scope: tuple[str, str] | None = None

    def track(self, ce_strike: float, pe_strike: float, now: datetime) -> bool:
        """Start (or refresh) tracking a pair. Returns True if it was newly registered."""
        key = (ce_strike, pe_strike)
        if key in self._pairs:
            self._pairs.move_to_end(key)
            return False
        self._pairs[key] = _Pair(
            ce_strike,
            pe_strike,
            now,
            CandleAggregator(
                symbol=f"{ce_strike:g}CE+{pe_strike:g}PE",
                intervals_minutes=self.intervals,
                timezone=self.timezone,
                session_start=self.session_start,
            ),
        )
        while len(self._pairs) > self.max_pairs:
            self._pairs.popitem(last=False)  # the least recently requested
        return True

    def process(self, chain: OptionChainSnapshot | None, timestamp: datetime) -> None:
        """Feed every tracked pair this tick's combined premium. A tick where either leg is
        missing from the chain or has no price feeds nothing: a gap is shown as a gap."""
        if chain is None:
            return
        scope = (chain.underlying, chain.expiry)
        if self._scope is not None and scope != self._scope:
            self._pairs.clear()
        self._scope = scope
        if not self._pairs:
            return
        legs = {leg.strike: leg for leg in chain.strikes}
        for pair in self._pairs.values():
            ce, pe = legs.get(pair.ce_strike), legs.get(pair.pe_strike)
            if ce is None or pe is None or ce.call_ltp <= 0 or pe.put_ltp <= 0:
                continue
            pair.aggregator.process_tick(PriceTick(symbol="PAIR", price=ce.call_ltp + pe.put_ltp, timestamp=timestamp))

    def get(self, ce_strike: float, pe_strike: float) -> tuple[datetime, dict[int, list[Candle]]] | None:
        """(tracked_since, {interval: candles incl. the active one}) or None if not tracked."""
        pair = self._pairs.get((ce_strike, pe_strike))
        if pair is None:
            return None
        self._pairs.move_to_end((ce_strike, pe_strike))
        out: dict[int, list[Candle]] = {}
        for interval in self.intervals:
            candles = list(pair.aggregator.completed_candles.get(interval, [])[-150:])
            active = pair.aggregator.active_candles.get(interval)
            if active is not None:
                candles.append(active)
            out[interval] = candles
        return pair.tracked_since, out

    def __len__(self) -> int:
        return len(self._pairs)
