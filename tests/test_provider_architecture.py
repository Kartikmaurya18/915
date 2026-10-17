"""
Provider-independent market data foundation: the normalized model, the
provider interface/registry, Dhan's mapping into the same model, Greeks
sourcing, the execution/risk boundaries, and the guarantees that the
frontend never sees provider payloads or credentials.
"""

import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from src.data_sources import (
    PROVIDERS,
    DataSource,
    MockDataSource,
    ProviderStatus,
    ReplayDataSource,
    UnavailableProvider,
    build_provider,
)
from src.data_sources.dhan import DhanMarketDataProvider, _unwrap_dhan
from src.engine import MarketEngine
from src.execution import OrderRequest, PaperExecutionClient, build_execution_client
from src.models import GREEKS_FROM_PROVIDER, Contract, OptionChainSnapshot, OptionLeg
from src.option_chain import find_atm_strike_delta
from src.option_chain.expiry import resolve_mock_expiry
from src.option_chain.greeks import ProviderGreeksEngine, build_greeks_engine
from src.option_chain.strike_straddle_candles import StrikeStraddleCandleEngine
from src.paper_trading import PaperTradingEngine
from src.risk import EXIT_STOP_LOSS, EXIT_TAKE_PROFIT, ExitRule, aggregate_pnl, evaluate_exit
from src.tick_validation import get_market_session_status
from src.timeutil import IST

ROOT = Path(__file__).resolve().parent.parent


def _engine_cfg(**overrides) -> dict:
    cfg = {
        "underlying": "NIFTY",
        "market_data_provider": "mock",
        "data_source": "mock",
        "poll_interval_seconds": 0.05,
        "candle_intervals": [1, 5, 15],
        "price_calculator": "mock",
    }
    cfg.update(overrides)
    return cfg


def _run_until_chain(engine: MarketEngine, timeout: float = 3.0) -> dict:
    engine.start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = engine.get_snapshot()
        if snap["option_chain"] and snap["atm"]:
            return snap
        time.sleep(0.05)
    raise AssertionError("engine produced no option chain in time")


# -- normalized model ------------------------------------------------------


def test_mock_provider_returns_normalized_chain_without_invented_fields():
    chain = MockDataSource(underlying="NIFTY", poll_interval=0.01).get_option_chain()
    assert isinstance(chain, OptionChainSnapshot)
    assert chain.provider == "MOCK"
    assert chain.exchange == "NSE"
    assert chain.expiries == [chain.expiry]
    assert chain.greeks_source is None  # a simulated feed has no exchange Greeks
    assert chain.timestamp.tzinfo is not None
    assert len(chain.strikes) == 11
    for leg in chain.strikes:
        assert leg.strike % 50 == 0
        # MOCK supplies no Greeks / IV / security id - they stay None until the calculator derives them.
        assert leg.call_delta is None and leg.put_delta is None
        assert leg.call_iv is None and leg.put_iv is None
        assert leg.call_instrument_id is None
        # It does supply a consistent quote: bid <= ltp <= ask, and OI with a previous-session baseline.
        assert 0 < leg.call_bid <= leg.call_ltp <= leg.call_ask and 0 < leg.put_bid <= leg.put_ltp <= leg.put_ask
        assert leg.call_oi > 0 and leg.call_oi_prev > 0


def test_sensex_mock_uses_bse_and_100_point_strikes():
    chain = MockDataSource(underlying="SENSEX", poll_interval=0.01).get_option_chain()
    assert chain.underlying == "SENSEX"
    assert chain.exchange == "BSE"
    strikes = [leg.strike for leg in chain.strikes]
    assert all(s % 100 == 0 for s in strikes)
    assert strikes == sorted(strikes) and strikes[1] - strikes[0] == 100
    # With calculated Greeks, ATM-by-delta finds the strike nearest 0.50 on a non-NIFTY chain.
    atm = find_atm_strike_delta(build_greeks_engine("calculated").enrich(chain), 0.5)
    assert abs(atm.strike - chain.underlying_ltp) <= 100


def test_replay_provider_is_simulated_and_normalized():
    src = ReplayDataSource(underlying="NIFTY", poll_interval=0.01)
    assert src.is_simulated and src.status() == ProviderStatus.SIMULATED
    assert src.get_option_chain().provider == "REPLAY"


def test_unknown_underlying_is_rejected_not_defaulted_to_nifty():
    with pytest.raises(ValueError, match="Unknown underlying"):
        MockDataSource(underlying="NOTANINDEX")


def test_chain_quote_and_contract_identity():
    ts = datetime(2026, 9, 15, 10, 0, tzinfo=IST)
    chain = OptionChainSnapshot(
        underlying="NIFTY",
        underlying_ltp=25010.0,
        expiry="2026-09-17",
        timestamp=ts,
        strikes=[OptionLeg(25000.0, 120.0, 110.0, call_oi=5, call_delta=0.52, call_iv=11.2, call_instrument_id="111")],
        provider="DHAN",
        exchange="NSE",
        greeks_source=GREEKS_FROM_PROVIDER,
    )
    q = chain.quote(25000.0, "ce")
    assert q.contract == Contract("NIFTY", "2026-09-17", 25000.0, "CE", "NSE", "111")
    assert q.contract.key == "NIFTY|2026-09-17|25000|CE"
    assert (q.ltp, q.open_interest, q.delta, q.iv, q.provider) == (120.0, 5, 0.52, 11.2, "DHAN")
    pe = chain.quote(25000.0, "PE")
    assert pe.delta is None and pe.greeks_source is None  # nothing supplied -> nothing claimed
    assert len(chain.quotes()) == 2
    assert chain.quote(99999.0, "CE") is None
    assert chain.trading_date == "2026-09-15"


# -- Dhan maps into the same model ----------------------------------------

# Shape returned by dhanhq 2.0.2: the library wraps the API body in
# {"status","remarks","data"}, and the option-chain body is itself
# {"data": {...}, "status": "success"}. Field names follow Dhan's v2
# option chain API. Values are test fixtures, not market data.
DHAN_WRAPPED_CHAIN = {
    "status": "success",
    "remarks": "",
    "data": {
        "status": "success",
        "data": {
            "last_price": 25012.5,
            "oc": {
                "25000.000000": {
                    "ce": {
                        "greeks": {"delta": 0.531, "theta": -12.0, "gamma": 0.001, "vega": 12.1},
                        "implied_volatility": 10.8,
                        "last_price": 118.0,
                        "oi": 100000,
                        "volume": 5000,
                        "top_bid_price": 117.9,
                        "top_ask_price": 118.2,
                    },
                    "pe": {"last_price": 104.0},  # deliberately sparse
                },
                "25050.000000": {"ce": {"last_price": 0}, "pe": {"last_price": 0}},  # no trades -> dropped
            },
        },
    },
}


@pytest.fixture
def dhan_provider(monkeypatch):
    monkeypatch.setenv("DHAN_CLIENT_ID", "test_client_123")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test_token_abc")
    with patch("src.data_sources.dhan.dhanhq") as mock_dhanhq:
        client = Mock()
        client.expiry_list.return_value = {
            "status": "success",
            "remarks": "",
            "data": {"status": "success", "data": ["2026-10-06", "2026-10-13"]},
        }
        client.option_chain.return_value = DHAN_WRAPPED_CHAIN
        mock_dhanhq.return_value = client
        provider = DhanMarketDataProvider(underlying="NIFTY")
        provider.OPTION_CHAIN_RATE_LIMIT = 0
        yield provider


def test_dhan_expiry_list_is_unwrapped_from_library_envelope(dhan_provider):
    assert dhan_provider.list_expiries() == ["2026-10-06", "2026-10-13"]
    assert dhan_provider._current_expiry == "2026-10-06"


def test_dhan_chain_maps_greeks_iv_bid_ask_into_normalized_model(dhan_provider):
    chain = dhan_provider.get_option_chain()
    assert chain.provider == "DHAN" and chain.exchange == "NSE"
    assert chain.underlying_ltp == 25012.5
    assert chain.expiry == "2026-10-06"
    assert chain.expiries == ["2026-10-06", "2026-10-13"]
    assert chain.greeks_source == GREEKS_FROM_PROVIDER
    assert [leg.strike for leg in chain.strikes] == [25000.0]
    leg = chain.strikes[0]
    assert (leg.call_ltp, leg.call_delta, leg.call_iv) == (118.0, 0.531, 10.8)
    assert (leg.call_bid, leg.call_ask, leg.call_oi, leg.call_volume) == (117.9, 118.2, 100000, 5000)
    # PE sent only a price: nothing else is invented (previously OI/volume became 0).
    assert leg.put_ltp == 104.0
    assert leg.put_oi is None and leg.put_volume is None and leg.put_delta is None and leg.put_iv is None


def test_dhan_chain_feeds_the_same_atm_logic(dhan_provider):
    atm = find_atm_strike_delta(dhan_provider.get_option_chain(), 0.5)
    assert atm.strike == 25000.0 and atm.greeks_source == GREEKS_FROM_PROVIDER


def test_dhan_status_reports_subscription_error(monkeypatch):
    monkeypatch.setenv("DHAN_CLIENT_ID", "test_client_123")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test_token_abc")
    with patch("src.data_sources.dhan.dhanhq") as mock_dhanhq:
        client = Mock()
        client.expiry_list.return_value = {
            "status": "failure",
            "remarks": {"error_code": "806", "error_message": "Data APIs not Subscribed"},
            "data": {"errorCode": "806"},
        }
        mock_dhanhq.return_value = client
        provider = DhanMarketDataProvider()
    assert provider.status() == ProviderStatus.ERROR
    assert "806" in provider.status_detail()
    assert "test_token_abc" not in provider.status_detail()


def test_dhan_rejects_unmapped_underlying(monkeypatch):
    monkeypatch.setenv("DHAN_CLIENT_ID", "test_client_123")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test_token_abc")
    with patch("src.data_sources.dhan.dhanhq"):
        with pytest.raises(ValueError, match="No Dhan security id"):
            DhanMarketDataProvider(underlying="FINNIFTY")


def test_dhan_sensex_uses_its_own_security_id(monkeypatch):
    monkeypatch.setenv("DHAN_CLIENT_ID", "test_client_123")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test_token_abc")
    with patch("src.data_sources.dhan.dhanhq") as mock_dhanhq:
        client = Mock()
        client.expiry_list.return_value = {"status": "success", "data": {"data": []}}
        mock_dhanhq.return_value = client
        provider = DhanMarketDataProvider(underlying="SENSEX")
    assert provider.security_id == 51 and provider.exchange == "BSE"
    assert client.expiry_list.call_args.kwargs["under_security_id"] == 51


def test_unwrap_dhan_handles_both_envelope_depths():
    assert _unwrap_dhan({"data": {"data": ["a"]}}) == ["a"]
    assert _unwrap_dhan({"data": {"last_price": 1, "oc": {}}}) == {"last_price": 1, "oc": {}}


# -- registry / configuration ---------------------------------------------


def test_registry_is_the_only_place_providers_are_constructed():
    offenders = []
    for path in (ROOT / "src").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("src/data_sources/"):
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\b(MockDataSource|ReplayDataSource|NSEDataSource|DhanMarketDataProvider|DhanDataSource)\(", text):
            offenders.append(rel)
    assert offenders == []


def test_registry_rejects_unknown_provider_name():
    with pytest.raises(ValueError, match="Unknown market data provider"):
        build_provider({"market_data_provider": "bloomberg"})
    assert set(PROVIDERS) == {"mock", "replay", "nse", "dhan"}


def test_market_data_provider_takes_precedence_over_legacy_data_source():
    src = build_provider({"market_data_provider": "replay", "data_source": "mock", "poll_interval_seconds": 1})
    assert isinstance(src, ReplayDataSource)


def test_load_config_reads_market_data_provider_env(monkeypatch):
    from src.config import load_config

    monkeypatch.setenv("MARKET_DATA_PROVIDER", "REPLAY")
    cfg = load_config()
    assert cfg["market_data_provider"] == cfg["data_source"] == "replay"


def test_failed_provider_degrades_to_error_placeholder_not_mock():
    def boom(cfg, underlying, poll):
        raise ValueError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN environment variables are required")

    with patch.dict(PROVIDERS, {"dhan": boom}):
        with pytest.raises(ValueError):
            build_provider({"market_data_provider": "dhan"})
        src = build_provider({"market_data_provider": "dhan"}, fail_soft=True)
    assert isinstance(src, UnavailableProvider)
    assert src.provider_name == "DHAN" and not src.is_simulated
    assert src.status() == ProviderStatus.ERROR
    assert list(src.stream_ticks()) == []


def test_engine_stays_up_and_reports_error_when_provider_cannot_start():
    def boom(cfg, underlying, poll):
        raise ConnectionError("network unreachable")

    with patch.dict(PROVIDERS, {"dhan": boom}):
        engine = MarketEngine(_engine_cfg(market_data_provider="dhan", data_source="dhan"))
    engine.start()
    time.sleep(0.2)
    snap = engine.get_snapshot()
    engine.close()
    assert snap["status"] == "ERROR"
    assert snap["provider"]["name"] == "DHAN"
    assert "network unreachable" in snap["provider"]["status_detail"]
    assert snap["price"] is None and snap["option_chain"] is None  # no fake data


def test_crashed_feed_is_reported_as_error():
    engine = MarketEngine(_engine_cfg())

    def broken_stream():
        raise RuntimeError("socket closed")
        yield  # pragma: no cover

    engine._source.stream_ticks = broken_stream
    engine.start()
    time.sleep(0.2)
    snap = engine.get_snapshot()
    engine.close()
    assert snap["status"] == "ERROR" and "socket closed" in snap["provider"]["status_detail"]


# -- MOCK end-to-end through the new interface -----------------------------


def test_mock_engine_snapshot_is_simulated_and_normalized():
    engine = MarketEngine(_engine_cfg(greeks_source="calculated"))
    try:
        snap = _run_until_chain(engine)
    finally:
        engine.close()
    assert snap["status"] == "SIMULATED" and snap["data_source"] == "mock"
    assert snap["provider"]["name"] == "MOCK" and snap["provider"]["is_simulated"] is True
    assert snap["provider"]["execution_mode"] == "paper"
    chain = snap["option_chain"]
    assert chain["provider"] == "MOCK" and chain["exchange"] == "NSE"
    assert chain["expiries"] == [chain["expiry"]]
    assert chain["timestamp"].endswith("+05:30")
    row = chain["strikes"][0]
    assert row["call_instrument_id"] is None  # MOCK has no broker security ids
    for key in ("call_iv", "put_iv", "call_bid", "put_ask", "call_gamma", "put_theta", "call_vega", "call_oi_change"):
        assert row[key] is not None, key
    assert chain["greeks_source"] == "calculated"
    assert snap["atm"]["method"] == "delta" and snap["atm"]["greeks_source"] == "calculated"
    assert set(snap["candles"]) == {"1", "5", "15"}


def test_snapshot_never_contains_dhan_credentials(monkeypatch):
    secret = "SENTINEL_TOKEN_should_never_leave_the_server"
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", secret)
    monkeypatch.setenv("DHAN_CLIENT_ID", "SENTINEL_CLIENT_ID")
    engine = MarketEngine(_engine_cfg())
    try:
        dumped = json.dumps(_run_until_chain(engine)) + json.dumps(engine.get_settings())
    finally:
        engine.close()
    assert secret not in dumped and "SENTINEL_CLIENT_ID" not in dumped


def test_frontend_does_not_read_raw_provider_payload_keys():
    js = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    # NSE/MOCK-style (records.data[].CE.lastPrice ...) and Dhan-style
    # (oc / last_price / implied_volatility) payload fields. `state.lastPrice`
    # is the UI's own previous-spot variable, not a payload read.
    raw_patterns = [
        r"(?<!state)\.lastPrice\b",
        r"\b(strikePrice|underlyingValue|expiryDates|openInterest|totalTradedVolume)\b",
        r"\.records\b",
        r"\.(CE|PE)\b",
        r"\b(last_price|implied_volatility|top_bid_price)\b",
        r"\.oc\b",
        r"DHAN_(ACCESS_TOKEN|CLIENT_ID)",
    ]
    for pattern in raw_patterns:
        assert not re.search(pattern, js), pattern


# -- execution boundary ------------------------------------------------------


def test_paper_order_goes_through_execution_interface_with_contract_identity():
    engine = MarketEngine(_engine_cfg())
    try:
        snap = _run_until_chain(engine)
        chain = snap["option_chain"]
        strike = snap["atm"]["strike"]
        pos = engine.open_paper_position("CE", strike, 25, "SELL", expiry=chain["expiry"], underlying="NIFTY")
        d = pos.to_dict()
        assert (d["underlying"], d["expiry"], d["exchange"], d["side"], d["quantity"]) == (
            "NIFTY", chain["expiry"], "NSE", "SELL", 25
        )
        assert d["simulated"] is True
        assert pos.contract.key == f"NIFTY|{chain['expiry']}|{strike:g}|CE"

        with pytest.raises(ValueError, match="Expiry"):
            engine.open_paper_position("PE", strike, 25, "BUY", expiry="01-Jan-2000")
        with pytest.raises(ValueError, match="Underlying"):
            engine.open_paper_position("PE", strike, 25, "BUY", underlying="SENSEX")

        closed = engine.close_paper_position(pos.id)
        assert closed.status == "EXITED" and closed.exit_reason == "MANUAL"
    finally:
        engine.close()


def test_only_paper_execution_is_available():
    book = PaperTradingEngine()
    assert isinstance(build_execution_client("paper", book), PaperExecutionClient)
    with pytest.raises(ValueError, match="not available"):
        build_execution_client("dhan", book)


def test_paper_execution_client_fills_at_reference_price():
    book = PaperTradingEngine()
    client = PaperExecutionClient(book)
    contract = Contract("SENSEX", "2026-10-01", 81500.0, "PE", "BSE", None)
    result = client.place_order(
        OrderRequest(contract, "BUY", 10, reference_price=250.0, timestamp=datetime(2026, 9, 15, 10, tzinfo=IST))
    )
    assert result.accepted and result.simulated and result.fill_price == 250.0
    assert result.position.underlying == "SENSEX" and result.position.exchange == "BSE"


# -- risk --------------------------------------------------------------------


def test_evaluate_exit_percent_and_amount_rules():
    rule = ExitRule(take_profit_percent=10, stop_loss_percent=5, take_profit_amount=500, stop_loss_amount=None)
    assert evaluate_exit(pnl=100, pnl_percent=10, rule=rule) == EXIT_TAKE_PROFIT
    assert evaluate_exit(pnl=500, pnl_percent=1, rule=rule) == EXIT_TAKE_PROFIT
    assert evaluate_exit(pnl=-50, pnl_percent=-5, rule=rule) == EXIT_STOP_LOSS
    assert evaluate_exit(pnl=-9999, pnl_percent=-4.9, rule=rule) is None  # ₹ SL disabled


def test_aggregate_pnl_across_legs():
    book = PaperTradingEngine()
    t0 = datetime(2026, 9, 15, 10, tzinfo=IST)
    ce = book.open_position("CE", 25000, 50, 100.0, t0, side="SELL")
    pe = book.open_position("PE", 25000, 50, 90.0, t0, side="SELL")
    ce.mark_to_market(96.0)  # +200
    pe.mark_to_market(93.0)  # -150
    out = aggregate_pnl([ce, pe])
    assert out["unrealized_pnl"] == pytest.approx(50.0)
    assert out["realized_pnl"] == 0.0
    assert out["entry_value"] == pytest.approx(9500.0)


# -- Greeks sourcing -----------------------------------------------------------


def test_greeks_engine_passes_provider_values_through_and_refuses_unknown_source():
    engine = build_greeks_engine("provider")
    assert isinstance(engine, ProviderGreeksEngine)
    chain = MockDataSource(poll_interval=0.01).get_option_chain()
    before = [(l.call_delta, l.call_iv) for l in chain.strikes]
    assert [(l.call_delta, l.call_iv) for l in engine.enrich(chain).strikes] == before  # provider mode never calculates
    with pytest.raises(ValueError, match="Unknown greeks_source"):
        build_greeks_engine("magic")


def test_atm_falls_back_to_price_when_no_provider_greeks():
    chain = OptionChainSnapshot(
        "NIFTY", 25010.0, "x", datetime(2026, 9, 15, 10, tzinfo=IST), [OptionLeg(25000.0, 100.0, 90.0)]
    )
    assert find_atm_strike_delta(chain) is None  # caller falls back to price-based


# -- candles: contract scoping -------------------------------------------------


def test_strike_candles_restart_when_expiry_changes():
    eng = StrikeStraddleCandleEngine([1])
    t0 = datetime(2026, 9, 15, 10, 0, 10, tzinfo=IST)

    def chain(expiry, ce):
        return OptionChainSnapshot("NIFTY", 25000.0, expiry, t0, [OptionLeg(25000.0, ce, 50.0)])

    eng.process(chain("E1", 100.0), t0)
    eng.process(chain("E1", 110.0), t0 + timedelta(seconds=5))
    eng.process(chain("E2", 40.0), t0 + timedelta(seconds=10))  # same strike, different contract
    active = eng.get_strike_builders(25000.0)["ce"].active_candles[1]
    assert (active.open, active.high, active.low, active.close) == (40.0, 40.0, 40.0, 40.0)


# -- IST calendar / session ----------------------------------------------------


def test_session_status_flags_exchange_holiday():
    holiday = datetime(2026, 10, 2, 11, 0, tzinfo=IST)  # Gandhi Jayanti, a Friday
    assert get_market_session_status(holiday) == "HOLIDAY"
    assert get_market_session_status(datetime(2026, 10, 1, 11, 0, tzinfo=IST)) == "OPEN"


def test_session_status_uses_ist_even_for_utc_input():
    from datetime import timezone

    utc = datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc)  # 09:30 IST
    assert get_market_session_status(utc) == "OPEN"


def test_mock_auto_expiry_moves_off_a_holiday():
    # 2026-03-26 (Thursday) is an NSE holiday -> previous trading day.
    assert resolve_mock_expiry(None, today=date(2026, 3, 23)) == "25-Mar-2026"
    assert resolve_mock_expiry(None, today=date(2026, 9, 21)) == "24-Sep-2026"
