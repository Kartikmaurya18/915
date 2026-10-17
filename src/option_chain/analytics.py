"""
Option pricing maths: Black-Scholes-Merton with a continuous carry (European
exercise, which is how NSE index options settle).

Pure functions, no I/O and no engine state, so every number can be checked against
a textbook value. The conventions are fixed here and nowhere else:

    delta   change in option price per 1.00 move of the underlying (calls 0..1, puts -1..0)
    gamma   change in delta per 1.00 move of the underlying
    theta   change in option price per CALENDAR DAY (negative for a long option)
    vega    change in option price per 1 percentage point of volatility
    iv      annualised volatility as a FRACTION (0.14 = 14 %), like every other field in the app

A value that cannot be computed honestly is None. In particular an implied volatility
is only returned when the price is inside the no-arbitrage band and the solver
converged; the engine never substitutes a plausible-looking number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.timeutil import IST

SECONDS_PER_YEAR = 365.0 * 24 * 3600
# NSE index options stop trading and settle at the close of the expiry day.
EXPIRY_TIME_IST = (15, 30)
# Below this much remaining life a Black-Scholes IV is dominated by tick size, not volatility.
MIN_YEARS = 5 * 60 / SECONDS_PER_YEAR
IV_MIN, IV_MAX = 0.005, 5.0

_SQRT_2PI = math.sqrt(2.0 * math.pi)


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float  # per 1 volatility point


def parse_expiry(expiry: str) -> Optional[datetime]:
    """The settlement instant (15:30 IST) of an expiry written "28-Oct-2026" or "2026-10-28"."""
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            day = datetime.strptime(expiry.strip(), fmt)
        except (ValueError, AttributeError):
            continue
        return day.replace(hour=EXPIRY_TIME_IST[0], minute=EXPIRY_TIME_IST[1], tzinfo=IST)
    return None


def years_to_expiry(expiry: str, now: datetime) -> Optional[float]:
    """Calendar years from `now` to settlement; None if the expiry is unparseable or already past."""
    end = parse_expiry(expiry)
    if end is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    seconds = (end - now).total_seconds()
    return seconds / SECONDS_PER_YEAR if seconds > 0 else None


def _d1_d2(spot: float, strike: float, t: float, r: float, q: float, vol: float) -> tuple[float, float]:
    root_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * vol * vol) * t) / (vol * root_t)
    return d1, d1 - vol * root_t


def price(option_type: str, spot: float, strike: float, t: float, r: float, q: float, vol: float) -> float:
    """Theoretical price. `option_type` is "CE" or "PE"."""
    if t <= 0 or vol <= 0:
        intrinsic = max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
        return intrinsic
    d1, d2 = _d1_d2(spot, strike, t, r, q, vol)
    disc_r, disc_q = math.exp(-r * t), math.exp(-q * t)
    if option_type == "CE":
        return spot * disc_q * _cdf(d1) - strike * disc_r * _cdf(d2)
    return strike * disc_r * _cdf(-d2) - spot * disc_q * _cdf(-d1)


def greeks(option_type: str, spot: float, strike: float, t: float, r: float, q: float, vol: float) -> Greeks:
    d1, d2 = _d1_d2(spot, strike, t, r, q, vol)
    root_t = math.sqrt(t)
    disc_r, disc_q = math.exp(-r * t), math.exp(-q * t)
    pdf1 = _pdf(d1)
    decay = -spot * disc_q * pdf1 * vol / (2.0 * root_t)
    if option_type == "CE":
        delta = disc_q * _cdf(d1)
        theta = decay - r * strike * disc_r * _cdf(d2) + q * spot * disc_q * _cdf(d1)
    else:
        delta = disc_q * (_cdf(d1) - 1.0)
        theta = decay + r * strike * disc_r * _cdf(-d2) - q * spot * disc_q * _cdf(-d1)
    return Greeks(
        delta=delta,
        gamma=disc_q * pdf1 / (spot * vol * root_t),
        theta=theta / 365.0,
        vega=spot * disc_q * pdf1 * root_t / 100.0,
    )


def implied_vol(
    option_type: str, market_price: float, spot: float, strike: float, t: float, r: float, q: float
) -> Optional[float]:
    """The volatility that reproduces `market_price`, or None when there is no honest answer.

    None when: inputs are non-positive, the option has under five minutes left, the price is at
    or below intrinsic value (no time value to explain) or above the no-arbitrage ceiling, or the
    solver does not converge. Bisection on a monotonic function, so it cannot diverge.
    """
    if not (market_price > 0 and spot > 0 and strike > 0) or t < MIN_YEARS:
        return None
    disc_r, disc_q = math.exp(-r * t), math.exp(-q * t)
    if option_type == "CE":
        lower, upper = max(spot * disc_q - strike * disc_r, 0.0), spot * disc_q
    else:
        lower, upper = max(strike * disc_r - spot * disc_q, 0.0), strike * disc_r
    if market_price <= lower + 1e-9 or market_price >= upper:
        return None
    lo, hi = IV_MIN, IV_MAX
    if price(option_type, spot, strike, t, r, q, hi) < market_price or price(option_type, spot, strike, t, r, q, lo) > market_price:
        return None  # outside what any volatility in the supported range can produce
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if price(option_type, spot, strike, t, r, q, mid) < market_price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return 0.5 * (lo + hi)


def implied_carry(
    spot: float, strike: float, call: float, put: float, t: float, r: float, default_q: float
) -> float:
    """Dividend yield implied by put-call parity at one strike (C - P = S e^{-qT} - K e^{-rT}).

    Using the market's own forward keeps call and put IVs consistent with each other instead of
    guessing a dividend yield. Falls back to `default_q` when the quotes cannot support it.
    """
    if not (call > 0 and put > 0 and spot > 0 and t >= MIN_YEARS):
        return default_q
    discounted_spot = call - put + strike * math.exp(-r * t)
    if discounted_spot <= 0:
        return default_q
    q = -math.log(discounted_spot / spot) / t
    return q if -0.10 <= q <= 0.10 else default_q  # outside +-10 % the quotes are stale or crossed
