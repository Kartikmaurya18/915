"""The internal Greeks calculator: fills only what the provider left empty, never invents."""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from src.data_sources.mock import MockDataSource
from src.models import GREEKS_CALCULATED, GREEKS_FROM_PROVIDER, GREEKS_MIXED, OptionChainSnapshot, OptionLeg
from src.option_chain import analytics
from src.option_chain.greeks import CalculatedGreeksEngine, build_greeks_engine
from src.serializers import serialize_chain
from src.timeutil import IST

NOW = datetime(2026, 10, 19, 11, 0, tzinfo=IST)
EXPIRY = "22-Oct-2026"
SPOT, R, Q = 24_850.0, 0.065, 0.012


def _chain(vol: float = 0.14, strikes=(24_600, 24_850, 25_100), expiry: str = EXPIRY, **leg_kwargs) -> OptionChainSnapshot:
    t = analytics.years_to_expiry(expiry, NOW) or 1e-4
    legs = [
        OptionLeg(
            float(k),
            round(analytics.price("CE", SPOT, k, t, R, Q, vol), 2),
            round(analytics.price("PE", SPOT, k, t, R, Q, vol), 2),
            **leg_kwargs,
        )
        for k in strikes
    ]
    return OptionChainSnapshot("NIFTY", SPOT, expiry, NOW, legs, expiries=[expiry])


def test_recovers_the_volatility_the_prices_were_built_from():
    chain = CalculatedGreeksEngine(R, Q).enrich(_chain(0.14))
    for leg in chain.strikes:
        assert leg.call_iv == pytest.approx(14.0, abs=0.3) and leg.put_iv == pytest.approx(14.0, abs=0.3)
        assert 0 < leg.call_delta < 1 and -1 < leg.put_delta < 0
        assert leg.call_gamma > 0 and leg.call_vega > 0 and leg.call_theta < 0
    assert chain.greeks_source == GREEKS_CALCULATED


def test_deep_in_the_money_side_takes_the_iv_of_the_out_of_the_money_side():
    chain = CalculatedGreeksEngine(R, Q).enrich(_chain(0.14, strikes=(24_300, 24_850)))
    deep = chain.strikes[0]
    assert deep.call_iv == deep.put_iv  # one volatility per strike (put-call parity)
    assert deep.call_iv == pytest.approx(14.0, abs=0.5)


def test_provider_values_always_win_and_only_gaps_are_filled():
    chain = _chain(0.14, call_iv=20.0, call_delta=0.9)
    enriched = CalculatedGreeksEngine(R, Q).enrich(chain)
    leg = enriched.strikes[1]
    assert leg.call_iv == 20.0 and leg.call_delta == 0.9  # untouched
    assert leg.put_iv == pytest.approx(14.0, abs=0.3)  # the gap was filled
    assert leg.call_gamma is not None  # derived from the provider's IV
    assert enriched.greeks_source == GREEKS_CALCULATED  # nothing was flagged as provider-sourced on the chain itself


def test_chain_reports_mixed_when_provider_and_calculator_both_contributed():
    chain = _chain(0.14, call_delta=0.5)
    chain.greeks_source = GREEKS_FROM_PROVIDER
    assert CalculatedGreeksEngine(R, Q).enrich(chain).greeks_source == GREEKS_MIXED


def test_a_complete_provider_chain_is_left_alone():
    full = dict(call_iv=14.0, put_iv=14.0, call_delta=0.5, put_delta=-0.5, call_gamma=1e-3, put_gamma=1e-3,
                call_theta=-15.0, put_theta=-15.0, call_vega=11.0, put_vega=11.0)
    chain = _chain(0.14, **full)
    chain.greeks_source = GREEKS_FROM_PROVIDER
    assert CalculatedGreeksEngine(R, Q).enrich(chain).greeks_source == GREEKS_FROM_PROVIDER


@pytest.mark.parametrize("expiry", ["19-Oct-2026", "garbage", ""])  # settles in 4.5 h is fine; past/unparseable are not
def test_past_or_unparseable_expiry_leaves_the_chain_untouched(expiry):
    chain = _chain(0.14, expiry=expiry)
    if expiry == "19-Oct-2026":  # still open at 11:00: calculable
        assert CalculatedGreeksEngine(R, Q).enrich(chain).strikes[0].call_iv is not None
        return
    enriched = CalculatedGreeksEngine(R, Q).enrich(chain)
    assert all(leg.call_iv is None and leg.call_delta is None for leg in enriched.strikes)
    assert enriched.greeks_source is None


def test_prices_with_no_time_value_stay_empty_instead_of_being_invented():
    chain = _chain(0.14, strikes=(24_850,))
    leg = chain.strikes[0]
    leg.call_ltp = leg.put_ltp = 0.0  # no quotes at all
    enriched = CalculatedGreeksEngine(R, Q).enrich(chain)
    assert enriched.strikes[0].call_iv is None and enriched.strikes[0].call_delta is None
    assert enriched.greeks_source is None


def test_serializer_exposes_the_new_fields_and_oi_change():
    chain = _chain(0.14, call_oi=1_200, call_oi_prev=1_000, put_oi=500)
    row = serialize_chain(CalculatedGreeksEngine(R, Q).enrich(chain), None)["strikes"][0]
    assert row["call_oi_change"] == 200
    assert row["put_oi_change"] is None  # no previous-session OI: not guessed
    assert row["call_theta"] < 0 and row["put_vega"] > 0 and row["call_gamma"] > 0


def test_unknown_source_is_rejected():
    with pytest.raises(ValueError):
        build_greeks_engine("tarot")
    assert build_greeks_engine("calculated").name == "calculated"


def test_the_mock_chain_is_internally_consistent():
    """The simulated chain must price like a real one, otherwise calculated Greeks would be meaningless."""
    for underlying in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
        chain = CalculatedGreeksEngine().enrich(MockDataSource(underlying=underlying).get_option_chain())
        ivs = [leg.call_iv for leg in chain.strikes]
        assert all(iv is not None and 5 < iv < 60 for iv in ivs), (underlying, ivs)
        calls = [leg.call_ltp for leg in chain.strikes]
        assert calls == sorted(calls, reverse=True)  # call premium falls as the strike rises
        puts = [leg.put_ltp for leg in chain.strikes]
        assert puts == sorted(puts)
        for leg in chain.strikes:
            assert leg.call_bid <= leg.call_ltp <= leg.call_ask and leg.put_bid <= leg.put_ltp <= leg.put_ask
            assert leg.call_oi_prev and leg.put_oi_prev


def test_enrichment_fits_comfortably_inside_a_tick():
    chain = MockDataSource().get_option_chain()
    chain.strikes = chain.strikes * 9  # ~100 strikes: far wider than any real NIFTY chain we poll
    engine = CalculatedGreeksEngine()
    start = time.perf_counter()
    for _ in range(5):
        for leg in chain.strikes:
            leg.call_iv = leg.put_iv = leg.call_delta = leg.put_delta = None
        engine.enrich(chain)
    per_chain_ms = (time.perf_counter() - start) / 5 * 1000
    assert per_chain_ms < 150, f"{per_chain_ms:.1f} ms for {len(chain.strikes)} strikes"
