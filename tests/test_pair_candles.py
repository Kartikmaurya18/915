"""Strangle candles: exact OHLC of CE@A + PE@B, tracked from the first request, never reconstructed."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.models import OptionChainSnapshot, OptionLeg
from src.option_chain.pair_candles import PairCandleEngine
from src.timeutil import IST

T0 = datetime(2026, 10, 19, 10, 0, 5, tzinfo=IST)


def chain(ticks_ce: dict[float, float], ticks_pe: dict[float, float], expiry: str = "22-Oct-2026", t: datetime = T0) -> OptionChainSnapshot:
    strikes = sorted(set(ticks_ce) | set(ticks_pe))
    legs = [OptionLeg(k, ticks_ce.get(k, 0.0), ticks_pe.get(k, 0.0)) for k in strikes]
    return OptionChainSnapshot("NIFTY", 24850.0, expiry, t, legs)


def engine(**kw) -> PairCandleEngine:
    return PairCandleEngine([1, 5], **kw)


def test_ohlc_is_the_ohlc_of_the_sum_not_the_sum_of_the_ohlcs():
    e = engine()
    e.track(24950, 24750, T0)
    # CE@24950 and PE@24750 move in opposite directions: sum highs and lows differ from the legs' own.
    series = [(100, 90), (110, 70), (80, 95), (90, 92)]
    for i, (ce, pe) in enumerate(series):
        e.process(chain({24950: ce, 24750: 0.0001}, {24950: 1, 24750: pe}), T0 + timedelta(seconds=10 * i))
    _, by = e.get(24950, 24750)
    candle = by[1][-1]
    sums = [ce + pe for ce, pe in series]
    assert (candle.open, candle.high, candle.low, candle.close) == (sums[0], max(sums), min(sums), sums[-1])
    assert max(sums) < max(c for c, _ in series) + max(p for _, p in series)  # the naive sum-of-highs would be wrong
    assert candle.tick_count == 4


def test_a_pair_has_no_history_before_it_was_requested():
    e = engine()
    for i in range(5):
        e.process(chain({24950: 100}, {24750: 80}), T0 + timedelta(seconds=10 * i))
    assert e.get(24950, 24750) is None  # nothing was recorded for an untracked pair
    later = T0 + timedelta(minutes=2)
    assert e.track(24950, 24750, later) is True
    e.process(chain({24950: 100}, {24750: 80}), later)
    since, by = e.get(24950, 24750)
    assert since == later
    assert by[1][0].open_time >= later.replace(second=0, microsecond=0)  # first candle starts when tracking did


def test_requesting_a_tracked_pair_again_does_not_restart_it():
    e = engine()
    assert e.track(24900, 24900, T0) is True
    assert e.track(24900, 24900, T0 + timedelta(hours=1)) is False
    e.process(chain({24900: 50}, {24900: 40}), T0)
    assert e.get(24900, 24900)[0] == T0


def test_a_missing_or_unpriced_leg_leaves_a_gap_instead_of_a_made_up_price():
    e = engine()
    e.track(24950, 24750, T0)
    e.process(chain({24950: 100}, {24750: 80}), T0)
    e.process(chain({24950: 100}, {24750: 0.0}), T0 + timedelta(seconds=10))  # PE has no price
    e.process(chain({24950: 100}, {}), T0 + timedelta(seconds=20))  # PE strike left the chain
    candle = e.get(24950, 24750)[1][1][-1]
    assert candle.tick_count == 1 and candle.close == 180


def test_memory_is_bounded_and_the_least_recently_requested_pair_goes_first():
    e = engine(max_pairs=3)
    for k in (1, 2, 3):
        e.track(k, k, T0)
    e.get(1, 1)  # touching a pair keeps it
    e.track(4, 4, T0)
    assert len(e) == 3
    assert e.get(2, 2) is None and e.get(1, 1) is not None and e.get(4, 4) is not None


def test_expiry_rollover_restarts_every_pair_rather_than_splicing_contracts():
    e = engine()
    e.track(24950, 24750, T0)
    e.process(chain({24950: 100}, {24750: 80}), T0)
    e.process(chain({24950: 30}, {24750: 20}, expiry="29-Oct-2026"), T0 + timedelta(seconds=10))
    assert len(e) == 0 and e.get(24950, 24750) is None


def test_completed_candles_are_kept_and_the_active_one_is_last():
    e = engine()
    e.track(24950, 24750, T0)
    for minute in range(4):
        e.process(chain({24950: 100 + minute}, {24750: 80}), T0 + timedelta(minutes=minute))
    candles = e.get(24950, 24750)[1][1]
    assert [c.is_complete for c in candles] == [True, True, True, False]
    assert [c.close for c in candles] == [180, 181, 182, 183]


# -- through the engine and the HTTP API -------------------------------------------------------


def test_engine_registers_on_first_request_and_rejects_strikes_outside_the_chain(client, contract):
    from src.api.server import engine as live_engine

    strike, expiry, _ = contract
    chain_now = live_engine._latest_chain
    lo, hi = chain_now.strikes[1].strike, chain_now.strikes[-2].strike
    first = live_engine.get_pair_candles(hi, lo)
    assert first["ce_strike"] == hi and first["pe_strike"] == lo and first["expiry"] == expiry
    assert first["by_interval"]["1"]["pair"] == [] or first["by_interval"]["1"]["pair"][-1]["is_complete"] is False
    live_engine.tick_once()
    live_engine.tick_once()
    again = live_engine.get_pair_candles(hi, lo)
    assert again["tracked_since"] == first["tracked_since"]
    pair = again["by_interval"]["1"]["pair"]
    assert pair, "ticks after registration must produce a candle"
    live_leg_sum = live_engine._latest_chain.get_leg(hi).call_ltp + live_engine._latest_chain.get_leg(lo).put_ltp
    assert pair[-1]["close"] == pytest.approx(live_leg_sum)
    assert live_engine.get_pair_candles(1.0, lo) is None and live_engine.get_pair_candles(hi, 9e9) is None


def test_endpoint_requires_sign_in_validates_and_serves_the_series(client, new_user, contract):
    from src.api.server import engine as live_engine

    _, headers = new_user()
    assert client.get("/api/pair-candles?ce_strike=24950&pe_strike=24750").status_code == 401
    assert client.get("/api/pair-candles?ce_strike=-5&pe_strike=24750", headers=headers).status_code == 422
    assert client.get("/api/pair-candles?ce_strike=1&pe_strike=2", headers=headers).status_code == 404
    legs = live_engine._latest_chain.strikes
    ok = client.get(f"/api/pair-candles?ce_strike={legs[-2].strike}&pe_strike={legs[1].strike}", headers=headers)
    assert ok.status_code == 200
    body = ok.json()
    assert set(body["by_interval"]) == {"1", "5", "15"} and "tracked_since" in body
