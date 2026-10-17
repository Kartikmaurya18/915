"""The pricing maths, pinned to independent references (parity, finite differences, round trips)."""

from __future__ import annotations

import math
from datetime import datetime

import pytest

from src.option_chain import analytics as a
from src.timeutil import IST

S, K, T, R, Q, VOL = 24_850.0, 24_900.0, 7 / 365, 0.065, 0.012, 0.14


def test_price_matches_a_textbook_value():
    # Hull, "Options, Futures and Other Derivatives": S=42 K=40 r=10% sigma=20% T=0.5 -> call 4.76, put 0.81
    assert a.price("CE", 42, 40, 0.5, 0.10, 0.0, 0.20) == pytest.approx(4.7594, abs=1e-3)
    assert a.price("PE", 42, 40, 0.5, 0.10, 0.0, 0.20) == pytest.approx(0.8086, abs=1e-3)


@pytest.mark.parametrize("strike", [24_000, 24_850, 25_600])
def test_put_call_parity_holds(strike):
    call, put = a.price("CE", S, strike, T, R, Q, VOL), a.price("PE", S, strike, T, R, Q, VOL)
    assert call - put == pytest.approx(S * math.exp(-Q * T) - strike * math.exp(-R * T), abs=1e-6)


@pytest.mark.parametrize("opt", ["CE", "PE"])
@pytest.mark.parametrize("strike", [24_500, 24_850, 25_300])
def test_greeks_agree_with_finite_differences(opt, strike):
    g = a.greeks(opt, S, strike, T, R, Q, VOL)
    ds, dv, dt = 0.5, 1e-4, 1e-5
    up, dn = a.price(opt, S + ds, strike, T, R, Q, VOL), a.price(opt, S - ds, strike, T, R, Q, VOL)
    mid = a.price(opt, S, strike, T, R, Q, VOL)
    assert g.delta == pytest.approx((up - dn) / (2 * ds), abs=1e-6)
    assert g.gamma == pytest.approx((up - 2 * mid + dn) / ds**2, rel=1e-3, abs=1e-8)
    vega = (a.price(opt, S, strike, T, R, Q, VOL + dv) - a.price(opt, S, strike, T, R, Q, VOL - dv)) / (2 * dv)
    assert g.vega == pytest.approx(vega / 100, rel=1e-4)
    theta = -(a.price(opt, S, strike, T + dt, R, Q, VOL) - a.price(opt, S, strike, T - dt, R, Q, VOL)) / (2 * dt)
    assert g.theta == pytest.approx(theta / 365, rel=1e-4)


def test_signs_and_ranges():
    call, put = a.greeks("CE", S, K, T, R, Q, VOL), a.greeks("PE", S, K, T, R, Q, VOL)
    assert 0 < call.delta < 1 and -1 < put.delta < 0
    assert call.gamma == pytest.approx(put.gamma)
    assert call.vega == pytest.approx(put.vega) and call.vega > 0
    assert call.theta < 0  # a long call bleeds time value


@pytest.mark.parametrize("opt", ["CE", "PE"])
@pytest.mark.parametrize("vol", [0.08, 0.14, 0.45, 1.2])
@pytest.mark.parametrize("strike", [23_000, 24_850, 26_500])
def test_implied_vol_round_trips(opt, vol, strike):
    p = a.price(opt, S, strike, T, R, Q, vol)
    iv = a.implied_vol(opt, p, S, strike, T, R, Q)
    lower = max(S * math.exp(-Q * T) - strike * math.exp(-R * T), 0.0) if opt == "CE" else max(strike * math.exp(-R * T) - S * math.exp(-Q * T), 0.0)
    if p - lower < 1e-3:  # no time value (deep in the money or worthless): the price says nothing about volatility
        assert iv is None or iv == pytest.approx(vol, abs=0.05)
    else:
        assert iv == pytest.approx(vol, abs=1e-4)


@pytest.mark.parametrize(
    "opt,market",
    [("CE", 50_000.0), ("PE", 0.0), ("PE", -5.0), ("CE", 24_850 - 24_000)],  # below intrinsic / above ceiling / non-positive
)
def test_implied_vol_is_none_when_there_is_no_honest_answer(opt, market):
    strike = 24_000 if market == 24_850 - 24_000 else K
    assert a.implied_vol(opt, market, S, strike, T, R, Q) is None


def test_implied_vol_is_none_with_minutes_to_expiry():
    assert a.implied_vol("CE", 5.0, S, K, 60 / a.SECONDS_PER_YEAR, R, Q) is None


def test_implied_carry_recovers_the_dividend_yield_from_parity():
    call, put = a.price("CE", S, K, T, R, Q, VOL), a.price("PE", S, K, T, R, Q, VOL)
    assert a.implied_carry(S, K, call, put, T, R, default_q=0.0) == pytest.approx(Q, abs=1e-6)


def test_implied_carry_falls_back_when_quotes_are_unusable():
    assert a.implied_carry(S, K, 0.0, 100.0, T, R, default_q=0.01) == 0.01
    assert a.implied_carry(S, K, 5_000.0, 1.0, T, R, default_q=0.01) == 0.01  # implies a +-10 % carry: stale/crossed quotes


def test_expiry_parsing_and_time_to_expiry():
    end = a.parse_expiry("28-Oct-2026")
    assert end == datetime(2026, 10, 28, 15, 30, tzinfo=IST)
    assert a.parse_expiry("2026-10-28") == end
    assert a.parse_expiry("garbage") is None
    now = datetime(2026, 10, 27, 15, 30, tzinfo=IST)
    assert a.years_to_expiry("28-Oct-2026", now) == pytest.approx(1 / 365)
    assert a.years_to_expiry("28-Oct-2026", datetime(2026, 10, 28, 16, 0, tzinfo=IST)) is None  # already settled
    assert a.years_to_expiry("28-Oct-2026", datetime(2026, 10, 27, 15, 30)) == pytest.approx(1 / 365)  # naive == IST wall clock
