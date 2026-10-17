"""
Where option Greeks come from.

Units (the same for every provider and for the calculator, so the UI never converts):
    iv     percentage points (14.2 means 14.2 %), as providers such as Dhan send it
    delta  per 1.00 move of the underlying;  gamma  delta per 1.00 move
    theta  per calendar day;                  vega   per 1 volatility point

Two engines, chosen by `greeks_source`:
  - "provider"   use exactly what the provider supplied (Dhan sends delta/gamma/theta/vega/IV).
                 MOCK and Replay supply none, so ATM selection falls back to price-based.
  - "calculated" the same, plus an internal Black-Scholes calculator (option_chain/analytics.py)
                 that fills ONLY the fields the provider left empty. Provider values always win.

`enrich(chain)` runs on every chain before ATM selection. A value that cannot be computed
honestly (no time value, expired, crossed quotes) stays None: nothing is ever invented.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod

from src.models import GREEKS_CALCULATED, GREEKS_FROM_PROVIDER, GREEKS_MIXED, OptionChainSnapshot, OptionLeg
from src.option_chain import analytics


class GreeksEngine(ABC):
    name: str = "abstract"

    @abstractmethod
    def enrich(self, chain: OptionChainSnapshot) -> OptionChainSnapshot:
        """Return the chain with any Greeks this engine is responsible for."""


class ProviderGreeksEngine(GreeksEngine):
    """Use exactly what the provider supplied — no calculation, no filling."""

    name = "provider"

    def enrich(self, chain: OptionChainSnapshot) -> OptionChainSnapshot:
        return chain


# Decimal places kept, chosen to be finer than anything the screen shows. Beyond that the extra digits are noise
# that changes on every tick, which would put every strike in every patch and inflate what each client downloads.
PRECISION = {"iv": 2, "delta": 4, "gamma": 6, "theta": 2, "vega": 2}


class CalculatedGreeksEngine(GreeksEngine):
    """Black-Scholes IV and Greeks for whatever the provider did not send."""

    name = "calculated"

    def __init__(self, risk_free_rate: float = 0.065, dividend_yield: float = 0.012):
        self.risk_free_rate = risk_free_rate
        self.dividend_yield = dividend_yield

    def _carry(self, chain: OptionChainSnapshot, t: float) -> float:
        """Dividend yield implied by parity at the strike nearest spot (see analytics.implied_carry)."""
        spot, r = chain.underlying_ltp, self.risk_free_rate
        near = min((leg for leg in chain.strikes if leg.call_ltp > 0 and leg.put_ltp > 0), key=lambda leg: abs(leg.strike - spot), default=None)
        if near is None:
            return self.dividend_yield
        return analytics.implied_carry(spot, near.strike, near.call_ltp, near.put_ltp, t, r, self.dividend_yield)

    def _strike_iv(self, leg: OptionLeg, chain: OptionChainSnapshot, t: float, q: float) -> float | None:
        """One volatility (percent) for the strike, read from its out-of-the-money option.

        For European options put-call parity makes the call and put IV at a strike equal, and the
        OTM option is the one that carries real time value, so its price is the better evidence.
        Falls back to the in-the-money side when the OTM quote is unusable.
        """
        spot, r = chain.underlying_ltp, self.risk_free_rate
        forward = spot * math.exp((r - q) * t)
        order = (("CE", leg.call_ltp), ("PE", leg.put_ltp)) if leg.strike >= forward else (("PE", leg.put_ltp), ("CE", leg.call_ltp))
        for opt, ltp in order:
            fraction = analytics.implied_vol(opt, ltp, spot, leg.strike, t, r, q)
            if fraction is not None:
                return fraction * 100.0
        return None

    def _fill_side(self, leg: OptionLeg, side: str, opt: str, chain: OptionChainSnapshot, t: float, q: float, strike_iv: float | None) -> int:
        """Fill the empty Greek fields of one side; returns how many it filled."""
        iv = getattr(leg, f"{side}_iv")
        filled = 0
        if iv is None:
            if strike_iv is None:
                return 0
            iv = strike_iv
            setattr(leg, f"{side}_iv", round(iv, PRECISION["iv"]))
            filled += 1
        wanted = [f for f in ("delta", "gamma", "theta", "vega") if getattr(leg, f"{side}_{f}") is None]
        if wanted:
            g = analytics.greeks(opt, chain.underlying_ltp, leg.strike, t, self.risk_free_rate, q, iv / 100.0)
            for field in wanted:
                setattr(leg, f"{side}_{field}", round(getattr(g, field), PRECISION[field]))
                filled += 1
        return filled

    def enrich(self, chain: OptionChainSnapshot) -> OptionChainSnapshot:
        t = analytics.years_to_expiry(chain.expiry, chain.timestamp)
        if t is None or chain.underlying_ltp <= 0 or not chain.strikes:
            return chain  # unknown, past or unparseable expiry: leave whatever the provider sent
        q = self._carry(chain, t)
        filled = 0
        for leg in chain.strikes:
            needs_iv = leg.call_iv is None or leg.put_iv is None
            strike_iv = self._strike_iv(leg, chain, t, q) if needs_iv else None
            filled += self._fill_side(leg, "call", "CE", chain, t, q, strike_iv)
            filled += self._fill_side(leg, "put", "PE", chain, t, q, strike_iv)
        if filled:
            chain.greeks_source = GREEKS_MIXED if chain.greeks_source == GREEKS_FROM_PROVIDER else GREEKS_CALCULATED
        return chain


GREEKS_ENGINES: dict[str, type[GreeksEngine]] = {
    "provider": ProviderGreeksEngine,
    "calculated": CalculatedGreeksEngine,
}


def build_greeks_engine(
    source: str | None = "provider", risk_free_rate: float = 0.065, dividend_yield: float = 0.012
) -> GreeksEngine:
    name = (source or "provider").lower()
    engine_cls = GREEKS_ENGINES.get(name)
    if engine_cls is None:
        raise ValueError(f"Unknown greeks_source {source!r}. Available: {sorted(GREEKS_ENGINES)}.")
    if engine_cls is CalculatedGreeksEngine:
        return CalculatedGreeksEngine(risk_free_rate, dividend_yield)
    return engine_cls()
