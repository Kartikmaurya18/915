"""
The single place a market data provider is chosen and constructed.

Selected by `market_data_provider` in config.yaml or the
MARKET_DATA_PROVIDER env var (legacy name: data_source / DATA_SOURCE) —
see src/config.py. Nothing else in the codebase branches on provider name.

Adding a provider = implement DataSource (src/data_sources/base.py) and add
one entry to PROVIDERS below.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from src.data_sources.base import DataSource, ProviderStatus
from src.data_sources.mock import MockDataSource
from src.data_sources.replay import DEFAULT_SEQUENCE, ReplayDataSource

log = logging.getLogger("market_data.registry")


def _mock(cfg: dict, underlying: str, poll: float) -> DataSource:
    return MockDataSource(
        underlying=underlying,
        poll_interval=poll,
        timezone=cfg.get("timezone", "Asia/Kolkata"),
        expiry=cfg.get("expiry"),
    )


def _replay(cfg: dict, underlying: str, poll: float) -> DataSource:
    return ReplayDataSource(
        underlying=underlying,
        poll_interval=poll,
        timezone=cfg.get("timezone", "Asia/Kolkata"),
        sequence=cfg.get("replay_sequence", DEFAULT_SEQUENCE),
        speed=float(cfg.get("replay_speed", 1.0)),
        expiry=cfg.get("expiry"),
    )


def live_data_enabled() -> bool:
    """Real market data is OFF unless an operator deliberately sets LIVE_DATA_ENABLED=true.

    Showing exchange data to users is governed by licences (the data vendor's and the exchange's) that this
    code cannot check, so the switch is explicit, off by default, and logged loudly when on. Mock and replay
    never need it."""
    return os.getenv("LIVE_DATA_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _disabled_live(name: str) -> Callable[[dict, str, float], DataSource]:
    def _raise(cfg: dict, underlying: str, poll: float) -> DataSource:
        raise RuntimeError(f"{name} live market provider is disabled. This deployment runs exclusively on MOCK data.")
    return _raise


def _dhan(cfg: dict, underlying: str, poll: float) -> DataSource:
    if not live_data_enabled():
        raise RuntimeError(
            "Dhan live market provider is disabled. This deployment runs exclusively on MOCK data. "
            "To use real data deliberately, set LIVE_DATA_ENABLED=true (see docs/LIVE_DATA.md for the licensing check first)."
        )
    from src.data_sources.dhan import DhanMarketDataProvider  # imported late: needs the optional dhanhq package

    log.warning(
        "LIVE market data is ENABLED (Dhan). Confirm your data licence permits displaying it to every user of this "
        "deployment before exposing it beyond yourself."
    )
    return DhanMarketDataProvider(underlying=underlying, poll_interval=poll)


PROVIDERS: dict[str, Callable[[dict, str, float], DataSource]] = {
    "mock": _mock,
    "replay": _replay,
    "nse": _disabled_live("NSE"),
    "dhan": _dhan,
}


def provider_key(cfg: dict) -> str:
    return str(cfg.get("market_data_provider") or cfg.get("data_source") or "mock").lower()


class UnavailableProvider(DataSource):
    """
    Stands in for a provider that could not be constructed (missing
    credentials, network down, ...), so the app still starts and the UI shows
    ERROR with the reason instead of the whole server failing to boot.
    It produces no data at all — deliberately NOT a fallback to MOCK, which
    would silently present simulated prices as if they were live.
    """

    def __init__(self, name: str, underlying: str, reason: str):
        self.provider_name = name.upper()
        self.underlying = underlying
        self._reason = reason

    def fetch_option_chain(self) -> dict:
        raise ConnectionError(self._reason)

    def get_underlying_ltp(self) -> float:
        raise ConnectionError(self._reason)

    def stream_ticks(self):
        return iter(())

    def status(self) -> ProviderStatus:
        return ProviderStatus.ERROR

    def status_detail(self) -> str | None:
        return self._reason


def build_provider(cfg: dict, fail_soft: bool = False) -> DataSource:
    """
    Construct the configured provider. An unknown provider name or invalid
    poll interval always raises (a config mistake should fail loudly). With
    `fail_soft`, an error while *constructing* a known provider returns an
    UnavailableProvider instead of raising.
    """
    underlying = str(cfg.get("underlying", "NIFTY")).upper()
    poll = float(cfg.get("poll_interval_seconds", 5))
    if poll <= 0:
        raise ValueError("poll_interval_seconds must be greater than zero.")
    name = provider_key(cfg)
    factory = PROVIDERS.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown market data provider: {name!r}. Use one of {sorted(PROVIDERS)}."
        )
    try:
        return factory(cfg, underlying, poll)
    except Exception as exc:
        if not fail_soft:
            raise
        # Exception text from provider constructors never includes secret
        # values (see DhanMarketDataProvider's credential diagnostics).
        return UnavailableProvider(name, underlying, f"{exc.__class__.__name__}: {exc}")
