"""
Where option Greeks (Delta, IV) come from — a seam, not a pricing model.

Today:
  - MOCK / Replay supply a simulated Delta (logistic curve around spot) and
    no IV.
  - Dhan's option chain API supplies Delta and IV per contract; the Dhan
    provider maps them into OptionLeg.call_delta / call_iv etc.
  - Nothing is calculated internally.

`GreeksEngine.enrich(chain)` runs on every chain before ATM selection, so
ATM-by-Delta and everything downstream is indifferent to whether Delta came
from the provider or from a calculator. A future internal engine (e.g.
Black-Scholes IV/Delta from LTP + spot + time-to-expiry + rate) subclasses
GreeksEngine, fills only the fields the provider left None, and sets
`chain.greeks_source = GREEKS_CALCULATED` — it is registered in
`build_greeks_engine()` below once it exists and has been validated.

Never fabricate Greeks: if neither the provider nor a real calculator
supplies a value, it stays None and ATM falls back to price-based.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.models import OptionChainSnapshot


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


GREEKS_ENGINES: dict[str, type[GreeksEngine]] = {
    "provider": ProviderGreeksEngine,
}


def build_greeks_engine(source: str | None = "provider") -> GreeksEngine:
    name = (source or "provider").lower()
    engine_cls = GREEKS_ENGINES.get(name)
    if engine_cls is None:
        raise ValueError(
            f"Unknown greeks_source {source!r}. Available: {sorted(GREEKS_ENGINES)}. "
            "An internal Greeks calculator is not implemented yet."
        )
    return engine_cls()
