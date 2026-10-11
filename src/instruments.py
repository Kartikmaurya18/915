"""
Provider-independent reference data for the index underlyings this system
trades options on. One place to look up an underlying's exchange and strike
spacing, so nothing else hard-codes NIFTY-only assumptions (a 50-point
strike step, NSE as the exchange, ...).

Provider-specific identifiers (e.g. Dhan security ids) deliberately do NOT
live here — each provider keeps its own mapping next to its own code.

Lot sizes are intentionally omitted: exchanges revise them periodically, so
they should come from the provider's instrument master, not a constant here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UnderlyingSpec:
    symbol: str
    exchange: str  # where the index / its options trade: "NSE" or "BSE"
    strike_step: float  # spacing between listed strikes


UNDERLYINGS: dict[str, UnderlyingSpec] = {
    "NIFTY": UnderlyingSpec("NIFTY", "NSE", 50),
    "BANKNIFTY": UnderlyingSpec("BANKNIFTY", "NSE", 100),
    "FINNIFTY": UnderlyingSpec("FINNIFTY", "NSE", 50),
    "SENSEX": UnderlyingSpec("SENSEX", "BSE", 100),
    "BANKEX": UnderlyingSpec("BANKEX", "BSE", 100),
}


def get_underlying_spec(symbol: str) -> UnderlyingSpec:
    spec = UNDERLYINGS.get((symbol or "").upper())
    if spec is None:
        raise ValueError(
            f"Unknown underlying {symbol!r}. Supported: {sorted(UNDERLYINGS)} "
            "(add it to src/instruments.py)."
        )
    return spec
