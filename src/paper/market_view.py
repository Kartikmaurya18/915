"""Where the paper-trading service gets prices: the hub's latest topic state.

Reading from the hub (not the engine) is what lets a gateway that does not run the
engine - and therefore holds no authoritative market state - still price a trade.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from src.models import Contract
from src.realtime.hub import Hub
from src.realtime.topics import index_topic, options_topic

MAX_QUOTE_AGE_SECONDS = 60.0  # refuse to fill against a feed that has gone quiet


class NoQuote(ValueError):
    """The contract cannot be priced right now (no chain yet, stale feed, unknown strike)."""


class HubMarketView:
    def __init__(self, hub_getter: Callable[[], Hub], underlying: str):
        self._hub = hub_getter
        self.underlying = underlying

    def _chain(self) -> dict:
        chain = self._hub().latest(options_topic(self.underlying))
        if not chain:
            raise NoQuote("Option chain not available yet; cannot open a paper position.")
        return chain

    def _atm_strike(self) -> float | None:
        index = self._hub().latest(index_topic(self.underlying)) or {}
        atm = index.get("atm") or {}
        return atm.get("strike")

    def latest_time(self) -> datetime:
        chain = self._hub().latest(options_topic(self.underlying)) or {}
        try:
            return datetime.fromisoformat(chain["timestamp"])
        except (KeyError, TypeError, ValueError):
            return datetime.now(timezone.utc)

    def leg_price(self, option_type: str, strike: float) -> float | None:
        """Current premium of one contract, or None if it is not in the chain."""
        chain = self._hub().latest(options_topic(self.underlying))
        if not chain:
            return None
        field = "call_ltp" if option_type == "CE" else "put_ltp"
        for row in chain.get("strikes", ()):
            if row.get("strike") == strike:
                return row.get(field)
        return None

    def quote(
        self, option_type: str, strike: float | None, expiry: str | None, underlying: str | None
    ) -> tuple[Contract, float, datetime]:
        option_type = (option_type or "").upper()
        if option_type not in ("CE", "PE"):
            raise NoQuote("option_type must be 'CE' or 'PE'.")
        chain = self._chain()
        age = self._hub().feed_age_ms()
        if age is not None and age > MAX_QUOTE_AGE_SECONDS * 1000:
            raise NoQuote("Market data is stale; cannot open a paper position.")
        if underlying is not None and underlying.upper() != str(chain.get("underlying", "")).upper():
            raise NoQuote(f"Underlying {underlying} is not the current chain's ({chain.get('underlying')}).")
        if expiry is not None and expiry != chain.get("expiry"):
            raise NoQuote(f"Expiry {expiry} is not the current chain's ({chain.get('expiry')}).")
        if strike is None:
            strike = self._atm_strike()
            if strike is None:
                raise NoQuote("ATM strike not available yet; cannot open a paper position.")
        row = next((r for r in chain.get("strikes", ()) if r.get("strike") == strike), None)
        if row is None:
            raise NoQuote(f"Strike {strike} not found in the current option chain.")
        ltp = row.get("call_ltp" if option_type == "CE" else "put_ltp")
        if ltp is None or ltp <= 0:
            raise NoQuote(f"No price for {option_type} {strike}.")
        contract = Contract(
            underlying=chain.get("underlying") or self.underlying,
            expiry=chain.get("expiry") or "",
            strike=float(strike),
            option_type=option_type,
            exchange=chain.get("exchange"),
            instrument_id=row.get("call_instrument_id" if option_type == "CE" else "put_instrument_id"),
        )
        return contract, float(ltp), self.latest_time()
