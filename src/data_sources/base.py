"""
The common market data interface every provider implements.

    MOCK / Replay / NSE / DHAN provider
                  │  (provider-specific payloads stay inside the provider)
                  ▼
    MarketDataProvider:  stream_ticks()        -> PriceTick
                         get_option_chain()    -> OptionChainSnapshot
                         get_underlying_ltp()  -> float
                         list_expiries()       -> list[str]
                         status()              -> ProviderStatus
                  │
                  ▼
    MarketEngine -> ATM / candles / straddle / paper trading / serializers / UI

The engine only ever calls the methods above, so adding a provider never
touches anything downstream of it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from src.models import OptionChainSnapshot


class ProviderStatus(str, Enum):
    """Connection/data state of a provider, shown as the dashboard badge."""

    CONNECTING = "CONNECTING"
    LIVE = "LIVE"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"
    SIMULATED = "SIMULATED"


class DataSource(ABC):
    """Abstract market data provider (see module docstring)."""

    #: Stable provider name, e.g. "MOCK", "DHAN". Shown in the UI.
    provider_name: str = "UNKNOWN"
    #: True for any provider whose prices are generated, not market-sourced.
    is_simulated: bool = False

    underlying: str = "NIFTY"

    @abstractmethod
    def fetch_option_chain(self) -> dict:
        """Return the provider's raw option chain payload. Provider-internal:
        the engine uses get_option_chain() instead."""

    @abstractmethod
    def get_underlying_ltp(self) -> float:
        """Return latest underlying last traded price."""

    @abstractmethod
    def stream_ticks(self):
        """Yield PriceTick objects continuously."""

    def get_option_chain(self) -> OptionChainSnapshot:
        """
        The normalized option chain. Default implementation adapts an
        NSE-style raw payload (`records.data[].CE/PE`), which is what MOCK,
        Replay and NSE all produce; a provider with a different native
        payload (e.g. Dhan) overrides this and maps its own fields directly.
        """
        from src.option_chain.atm import parse_option_chain

        chain = parse_option_chain(self.fetch_option_chain(), self.underlying)
        chain.provider = self.provider_name
        return chain

    def list_expiries(self) -> list[str]:
        """Every expiry this provider can currently price, nearest first."""
        return []

    def status(self) -> ProviderStatus:
        return ProviderStatus.SIMULATED if self.is_simulated else ProviderStatus.LIVE

    def status_detail(self) -> str | None:
        """Optional human-readable reason for the current status (e.g. an
        auth or subscription error). Must never contain credentials."""
        return None

    def credentials_expire_at(self):
        """When this provider's access credentials stop working (a timezone-aware datetime), or None if
        unknown or they do not expire. Lets the UI warn before a feed is lost mid-session."""
        return None

    def disconnect(self) -> None:
        """Release connections. No-op for providers that hold none."""


# The name the rest of the codebase should use going forward; DataSource is
# kept as the original name for backward compatibility.
MarketDataProvider = DataSource
