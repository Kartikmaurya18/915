"""The live-feed diagnostic's comparison logic, exercised on a mock chain (no network, no credentials)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from src.data_sources.mock import MockDataSource
from src.option_chain.greeks import CalculatedGreeksEngine

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_live_feed.py"
spec = importlib.util.spec_from_file_location("check_live_feed", SCRIPT)
check_live_feed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_live_feed)


def provider_like_chain():
    """A chain that carries Greeks the way a provider would (here: computed by the calculator itself)."""
    return CalculatedGreeksEngine().enrich(MockDataSource().get_option_chain())


def test_identical_greeks_compare_as_identical():
    result = check_live_feed.compare_greeks(provider_like_chain())
    assert set(result) == {"iv", "delta", "gamma", "theta", "vega"}
    for stats in result.values():
        assert stats["mean_abs_diff"] == pytest.approx(0, abs=1e-6)
        assert stats["median_ratio_provider_over_mine"] == pytest.approx(1.0, abs=1e-6)


def test_a_unit_mismatch_shows_up_as_a_ratio():
    """If a provider sent IV as a fraction (0.14) where we use percent (14.0), the ratio exposes it at once."""
    chain = provider_like_chain()
    for leg in chain.strikes:
        leg.call_iv = leg.call_iv / 100 if leg.call_iv else leg.call_iv
        leg.put_iv = leg.put_iv / 100 if leg.put_iv else leg.put_iv
    stats = check_live_feed.compare_greeks(chain)["iv"]
    assert stats["median_ratio_provider_over_mine"] == pytest.approx(0.01, rel=0.05)
    assert stats["mean_abs_diff"] > 5


def test_comparison_does_not_modify_the_chain_it_is_given():
    chain = provider_like_chain()
    before = [(leg.call_iv, leg.put_delta, leg.call_theta) for leg in chain.strikes]
    check_live_feed.compare_greeks(chain)
    assert [(leg.call_iv, leg.put_delta, leg.call_theta) for leg in chain.strikes] == before


def test_nothing_to_compare_when_the_provider_sent_no_greeks():
    chain = MockDataSource().get_option_chain()  # raw mock: no Greeks
    assert check_live_feed.compare_greeks(chain) == {}


def test_field_coverage_counts_what_the_provider_sent():
    raw = MockDataSource().get_option_chain()
    cov = check_live_feed.field_coverage(raw)
    n = len(raw.strikes)
    assert cov["last price"] == (n, n) and cov["bid/ask"] == (n, n) and cov["open interest"] == (n, n)
    assert cov["implied volatility"] == (0, n) and cov["delta"] == (0, n)
    assert check_live_feed.field_coverage(provider_like_chain())["gamma"] == (n, n)


def test_next_open_is_a_trading_day_at_0915():
    text = check_live_feed.next_open_text()
    assert text.endswith("09:15 IST")


def test_the_script_documents_itself_and_does_not_run_anything_on_import():
    out = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "Read-only check" in out.stdout
