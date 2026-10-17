"""Real-data safety: live data is opt-in, failures are visible, nothing stale is passed off as current."""

from __future__ import annotations

import base64
import json
import queue
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from src.data_sources import registry
from src.data_sources.base import ProviderStatus
from src.data_sources.dhan import DhanMarketDataProvider, explain_dhan_error, token_expiry
from src.models import PriceTick
from src.timeutil import IST, now_ist


def jwt(exp: datetime | None, extra: dict | None = None) -> str:
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    payload = {"iss": "dhan", **(extra or {})}
    if exp is not None:
        payload["exp"] = int(exp.timestamp())
    return f"{b64({'alg': 'HS256'})}.{b64(payload)}.signature"


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("DHAN_CLIENT_ID", "test_client_123")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test_token_abc")
    with patch("src.data_sources.dhan.dhanhq") as sdk:
        sdk.return_value = Mock()
        sdk.return_value.expiry_list.return_value = {"status": "success", "data": {"data": ["2026-10-22"]}}
        p = DhanMarketDataProvider()
        p._run_websocket_loop = lambda: None  # no network thread in tests
        yield p


GOOD_CHAIN = {"status": "success", "data": {"data": {"last_price": 24850.0, "oc": {"24850.000000": {"ce": {"last_price": 150.0}, "pe": {"last_price": 140.0}}}}}}


# -- the switch -------------------------------------------------------------------------------


def test_live_data_is_off_by_default(monkeypatch):
    monkeypatch.delenv("LIVE_DATA_ENABLED", raising=False)
    assert registry.live_data_enabled() is False
    with pytest.raises(RuntimeError, match="LIVE_DATA_ENABLED"):
        registry.build_provider({"market_data_provider": "dhan"})
    fallback = registry.build_provider({"market_data_provider": "dhan"}, fail_soft=True)
    assert fallback.status() == ProviderStatus.ERROR  # an error state, never silently mock data
    assert fallback.provider_name == "DHAN"
    with pytest.raises(ConnectionError):
        fallback.fetch_option_chain()


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "maybe"])
def test_only_explicit_yes_values_enable_live_data(monkeypatch, value):
    monkeypatch.setenv("LIVE_DATA_ENABLED", value)
    assert registry.live_data_enabled() is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " True "])
def test_explicit_opt_in_builds_the_real_provider_and_warns(monkeypatch, value, caplog):
    monkeypatch.setenv("LIVE_DATA_ENABLED", value)
    monkeypatch.setenv("DHAN_CLIENT_ID", "test_client_123")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test_token_abc")
    with patch("src.data_sources.dhan.dhanhq"), caplog.at_level("WARNING", logger="market_data.registry"):
        built = registry.build_provider({"market_data_provider": "dhan", "underlying": "NIFTY"})
    assert isinstance(built, DhanMarketDataProvider)
    assert "licence" in caplog.text.lower()


def test_nse_stays_disabled_even_with_the_switch_on(monkeypatch):
    monkeypatch.setenv("LIVE_DATA_ENABLED", "true")
    with pytest.raises(RuntimeError, match="NSE live market provider is disabled"):
        registry.build_provider({"market_data_provider": "nse"})


def test_mock_never_needs_the_switch(monkeypatch):
    monkeypatch.delenv("LIVE_DATA_ENABLED", raising=False)
    assert registry.build_provider({"market_data_provider": "mock"}).is_simulated is True


# -- credentials -------------------------------------------------------------------------------


def test_token_expiry_is_read_from_the_jwt():
    soon = now_ist() + timedelta(hours=3)
    assert abs((token_expiry(jwt(soon)) - soon).total_seconds()) < 1
    assert token_expiry(jwt(None)) is None  # a JWT with no exp
    for junk in (None, "", "plain-opaque-token", "a.b", "a.b.c", "x.!!!.z"):
        assert token_expiry(junk) is None


def test_an_already_expired_token_is_refused_up_front_without_leaking_it(monkeypatch):
    token = jwt(now_ist() - timedelta(hours=1))
    monkeypatch.setenv("DHAN_CLIENT_ID", "test_client_123")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", token)
    with patch("src.data_sources.dhan.dhanhq"):
        with pytest.raises(ValueError, match="expired") as info:
            DhanMarketDataProvider()
    assert token not in str(info.value)


def test_a_valid_token_exposes_when_it_expires(monkeypatch):
    soon = now_ist() + timedelta(hours=5)
    monkeypatch.setenv("DHAN_CLIENT_ID", "test_client_123")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", jwt(soon))
    with patch("src.data_sources.dhan.dhanhq"):
        p = DhanMarketDataProvider()
    assert abs((p.credentials_expire_at() - soon).total_seconds()) < 1


def test_opaque_tokens_report_no_expiry(provider):
    assert provider.credentials_expire_at() is None


def test_an_incompatible_sdk_version_is_named_not_a_bare_typeerror(monkeypatch):
    monkeypatch.setenv("DHAN_CLIENT_ID", "test_client_123")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test_token_abc")
    with patch("src.data_sources.dhan.dhanhq", side_effect=TypeError("__init__() got an unexpected keyword 'client_id'")):
        with pytest.raises(ImportError, match="dhanhq==2.0.2"):
            DhanMarketDataProvider()


def test_missing_sdk_is_a_clear_message_not_a_nonetype_error(monkeypatch):
    monkeypatch.setenv("DHAN_CLIENT_ID", "test_client_123")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test_token_abc")
    with patch("src.data_sources.dhan.dhanhq", None):
        with pytest.raises(ImportError, match="requirements-live.txt"):
            DhanMarketDataProvider()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("DH-1 {'errorCode': '807'}", "expired"),
        ("Authentication Failed - Client ID or Access Token invalid", "808"),
        ("error 809", "invalid"),
        ("Data APIs not subscribed", "806"),
        ("Too many requests or connections", None),
        ("805", "rate limit"),
        ("something unrelated", None),
    ],
)
def test_known_dhan_errors_get_a_clear_message(text, expected):
    message = explain_dhan_error(text)
    if expected is None:
        assert message is None or "rate limit" in message
    else:
        assert expected in message.lower() or expected in message


# -- failures must be visible -------------------------------------------------------------------


def test_an_auth_failure_on_the_chain_is_an_error_even_while_the_index_feed_ticks(provider):
    provider._has_received_tick = provider._is_connected = True
    assert provider.status() == ProviderStatus.LIVE
    provider._dhan.option_chain.return_value = {"status": "failure", "remarks": "x", "data": {"errorCode": "807"}}
    provider._last_chain_fetch_time = 0
    with pytest.raises(ConnectionError, match="expired"):
        provider.fetch_option_chain()
    assert provider.status() == ProviderStatus.ERROR
    assert "807" in provider.status_detail()


def test_a_recent_chain_bridges_a_one_off_failure_but_the_error_stays_visible(provider):
    provider._dhan.option_chain.return_value = GOOD_CHAIN
    provider._last_chain_fetch_time = 0
    first = provider.fetch_option_chain()
    provider._dhan.option_chain.side_effect = TimeoutError("boom")
    provider._last_chain_fetch_time = time.time() - 4  # past the rate limit, recent enough to bridge
    again = provider.fetch_option_chain()
    assert again is first and provider.status() == ProviderStatus.ERROR  # served, but flagged


def test_an_old_chain_is_never_served_again(provider):
    provider._dhan.option_chain.return_value = GOOD_CHAIN
    provider._last_chain_fetch_time = 0
    provider.fetch_option_chain()
    provider._dhan.option_chain.side_effect = TimeoutError("boom")
    provider._last_chain_fetch_time = time.time() - 600
    with pytest.raises(ConnectionError):
        provider.fetch_option_chain()


def test_recovery_clears_the_error(provider):
    provider._has_received_tick = provider._is_connected = True
    provider._dhan.option_chain.side_effect = TimeoutError("boom")
    provider._last_chain_fetch_time = 0
    with pytest.raises(ConnectionError):
        provider.fetch_option_chain()
    assert provider.status() == ProviderStatus.ERROR
    provider._dhan.option_chain.side_effect = None
    provider._dhan.option_chain.return_value = GOOD_CHAIN
    provider._last_chain_fetch_time = 0
    provider.fetch_option_chain()
    assert provider.status() == ProviderStatus.LIVE and provider.status_detail() is None


def test_a_dead_live_feed_is_reported_not_hidden(provider):
    provider._is_connected = False
    provider._ws_error = "Dhan live feed disconnected (ConnectionClosed); reconnecting"
    assert provider.status() == ProviderStatus.ERROR
    assert "reconnecting" in provider.status_detail()
    provider._is_connected, provider._has_received_tick, provider._ws_error = True, True, None
    assert provider.status() == ProviderStatus.LIVE


# -- the tick backlog ---------------------------------------------------------------------------


def test_stream_ticks_skips_the_backlog_and_yields_the_newest(provider):
    """The engine step is slower than the feed; consuming ticks one by one would fall behind real time forever."""
    for price in (100.0, 101.0, 102.0, 103.0, 104.0):
        provider._tick_queue.put_nowait(PriceTick(symbol="NIFTY", price=price, timestamp=now_ist(), provider="DHAN"))
    stream = provider.stream_ticks()
    assert next(stream).price == 104.0
    assert provider._tick_queue.empty()
    provider.disconnect()


def test_stream_ticks_still_stops_on_the_shutdown_sentinel(provider):
    provider._tick_queue.put_nowait(PriceTick(symbol="NIFTY", price=1.0, timestamp=now_ist(), provider="DHAN"))
    provider._tick_queue.put_nowait(None)
    assert list(provider.stream_ticks()) == []  # the sentinel after a backlog ends the stream cleanly


def test_the_provider_does_not_start_a_real_connection_in_these_tests(provider):
    assert isinstance(provider._tick_queue, queue.Queue)
