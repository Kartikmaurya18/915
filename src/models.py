from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


"""
Normalized, provider-independent market data model.

Every market data provider (MOCK, Replay, NSE, Dhan, ...) produces these
objects — nothing downstream (ATM, candles, straddle, paper trading, risk,
serializers, frontend) ever sees a provider's raw payload. Optional fields
stay None when a provider doesn't supply them; they are never filled with
invented values.
"""

OPTION_TYPES = ("CE", "PE")

# Where a chain's Greeks came from: supplied by the provider's own feed, or
# computed by an internal engine (see src/option_chain/greeks.py). None when
# the chain carries no Greeks at all.
GREEKS_FROM_PROVIDER = "provider"
GREEKS_CALCULATED = "calculated"
# The provider sent some Greeks and the internal calculator derived the rest.
GREEKS_MIXED = "provider+calculated"


@dataclass
class PriceTick:
    """Single price observation from the market."""

    symbol: str
    price: float
    timestamp: datetime
    volume: Optional[float] = None
    provider: Optional[str] = None  # e.g. "MOCK", "DHAN"
    exchange: Optional[str] = None  # e.g. "NSE", "BSE"
    instrument_id: Optional[str] = None  # broker security id, when the provider has one


@dataclass(frozen=True)
class Contract:
    """
    Provider-independent identity of one option contract. This — not a
    provider's security id — is what selection, paper trading, and any
    future strategy/risk/execution layer refer to. `instrument_id` is carried
    along when a provider supplies one (needed for broker order routing
    later) but is never required to identify the contract.
    """

    underlying: str
    expiry: str
    strike: float
    option_type: str  # "CE" | "PE"
    exchange: Optional[str] = None
    instrument_id: Optional[str] = None

    @property
    def key(self) -> str:
        return f"{self.underlying}|{self.expiry}|{self.strike:g}|{self.option_type}"


@dataclass
class OptionQuote:
    """One contract's normalized market data at a point in time."""

    contract: Contract
    ltp: float
    timestamp: datetime
    provider: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    delta: Optional[float] = None
    iv: Optional[float] = None
    greeks_source: Optional[str] = None


@dataclass
class OptionLeg:
    """One strike row of an option chain: the CE and PE side by side."""

    strike: float
    call_ltp: float
    put_ltp: float
    call_oi: Optional[int] = None
    put_oi: Optional[int] = None
    call_volume: Optional[int] = None
    put_volume: Optional[int] = None
    call_delta: Optional[float] = None
    put_delta: Optional[float] = None  # signed, roughly -1..0
    call_iv: Optional[float] = None
    put_iv: Optional[float] = None
    call_bid: Optional[float] = None
    call_ask: Optional[float] = None
    put_bid: Optional[float] = None
    put_ask: Optional[float] = None
    call_instrument_id: Optional[str] = None
    put_instrument_id: Optional[str] = None
    # Second-order Greeks (theta per calendar day, vega per volatility point) and the previous
    # session's open interest, so the change since yesterday's close can be shown. All None unless
    # a provider sent them or the internal calculator derived them (see option_chain/greeks.py).
    call_gamma: Optional[float] = None
    put_gamma: Optional[float] = None
    call_theta: Optional[float] = None
    put_theta: Optional[float] = None
    call_vega: Optional[float] = None
    put_vega: Optional[float] = None
    call_oi_prev: Optional[int] = None
    put_oi_prev: Optional[int] = None

    def ltp(self, option_type: str) -> float:
        return self.call_ltp if option_type.upper() == "CE" else self.put_ltp


@dataclass
class OptionChainSnapshot:
    underlying: str
    underlying_ltp: float
    expiry: str  # the expiry this chain's strikes are priced for
    timestamp: datetime
    strikes: list[OptionLeg] = field(default_factory=list)
    # Configurable expiry field for flexibility
    expiry_type: Optional[str] = None  # e.g., "weekly", "monthly"
    provider: str = "UNKNOWN"
    exchange: Optional[str] = None
    expiries: list[str] = field(default_factory=list)  # every expiry the provider offers
    greeks_source: Optional[str] = None  # GREEKS_FROM_PROVIDER | GREEKS_CALCULATED | None

    @property
    def trading_date(self) -> str:
        """Asia/Kolkata trading date ("YYYY-MM-DD") this chain belongs to."""
        from src.timeutil import trading_date_str

        return trading_date_str(self.timestamp)

    def get_leg(self, strike: float) -> Optional[OptionLeg]:
        for leg in self.strikes:
            if leg.strike == strike:
                return leg
        return None

    def contract(self, strike: float, option_type: str) -> Optional[Contract]:
        leg = self.get_leg(strike)
        if leg is None:
            return None
        option_type = option_type.upper()
        return Contract(
            underlying=self.underlying,
            expiry=self.expiry,
            strike=leg.strike,
            option_type=option_type,
            exchange=self.exchange,
            instrument_id=leg.call_instrument_id if option_type == "CE" else leg.put_instrument_id,
        )

    def quote(self, strike: float, option_type: str) -> Optional[OptionQuote]:
        """The normalized quote for one contract in this chain, or None if
        the strike isn't present."""
        contract = self.contract(strike, option_type)
        if contract is None:
            return None
        leg = self.get_leg(strike)
        is_call = contract.option_type == "CE"
        delta = leg.call_delta if is_call else leg.put_delta
        iv = leg.call_iv if is_call else leg.put_iv
        return OptionQuote(
            contract=contract,
            ltp=leg.call_ltp if is_call else leg.put_ltp,
            timestamp=self.timestamp,
            provider=self.provider,
            bid=leg.call_bid if is_call else leg.put_bid,
            ask=leg.call_ask if is_call else leg.put_ask,
            volume=leg.call_volume if is_call else leg.put_volume,
            open_interest=leg.call_oi if is_call else leg.put_oi,
            delta=delta,
            iv=iv,
            greeks_source=self.greeks_source if (delta is not None or iv is not None) else None,
        )

    def quotes(self) -> list[OptionQuote]:
        """Every CE and PE quote in the chain — the flat, per-contract view
        a strategy or risk engine consumes."""
        out: list[OptionQuote] = []
        for leg in self.strikes:
            for option_type in OPTION_TYPES:
                q = self.quote(leg.strike, option_type)
                if q is not None:
                    out.append(q)
        return out


@dataclass
class ATMResult:
    """At-The-Money strike and derived values."""

    strike: float
    call_ltp: float
    put_ltp: float
    straddle_premium: float  # Call + Put at ATM
    underlying_ltp: float
    distance_from_spot: float  # |strike - spot|
    method: str = "price"  # "price" (nearest to spot) or "delta" (Greeks-based)
    call_delta: Optional[float] = None
    put_delta: Optional[float] = None
    delta_threshold: Optional[float] = None  # target |delta|, e.g. 0.50
    price_based_strike: Optional[float] = None  # nearest-to-spot strike, for comparison
    greeks_source: Optional[str] = None  # where the Delta used came from (provider/calculated)


@dataclass
class Candle:
    """OHLC candlestick for a given interval."""

    symbol: str
    interval_minutes: int
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_count: int = 0
    is_complete: bool = False

    def update(self, price: float) -> None:
        if self.tick_count == 0:
            self.open = price
            self.high = price
            self.low = price
        else:
            self.high = max(self.high, price)
            self.low = min(self.low, price)
        self.close = price
        self.tick_count += 1
