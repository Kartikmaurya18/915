from __future__ import annotations

import time
from datetime import datetime

import requests

from src.data_sources.base import DataSource, ProviderStatus
from src.models import PriceTick
from src.timeutil import now_ist

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}


class NSEDataSource(DataSource):
    """
    Fetches live option chain data from NSE India.
    Requires network access; NSE may rate-limit or block repeated requests.
    """

    provider_name = "NSE"

    OPTION_CHAIN_URL = "https://www.nseindia.com/api/option-chain-indices"
    SESSION_URL = "https://www.nseindia.com/option-chain"

    def __init__(self, underlying: str = "NIFTY", poll_interval: float = 5.0):
        self.underlying = underlying.upper()
        self.poll_interval = poll_interval
        self._session = requests.Session()
        self._session.headers.update(NSE_HEADERS)
        self._last_chain: dict | None = None
        self._last_error: str | None = None
        self._bootstrap_session()

    def _bootstrap_session(self) -> None:
        """NSE requires a cookie from the main page before API calls work."""
        try:
            self._session.get(self.SESSION_URL, timeout=15)
        except requests.RequestException as exc:
            raise ConnectionError(
                f"Could not reach NSE ({self.SESSION_URL}): {exc}"
            ) from exc

    def fetch_option_chain(self) -> dict:
        params = {"symbol": self.underlying}
        try:
            response = self._session.get(
                self.OPTION_CHAIN_URL, params=params, timeout=15
            )
            if response.status_code == 403:
                self._bootstrap_session()
                response = self._session.get(
                    self.OPTION_CHAIN_URL, params=params, timeout=15
                )
            response.raise_for_status()
            self._last_chain = response.json()
            self._last_error = None
            return self._last_chain
        except requests.RequestException as exc:
            self._last_error = f"NSE option chain fetch failed: {exc}"
            raise ConnectionError(self._last_error) from exc

    def list_expiries(self) -> list[str]:
        if self._last_chain is None:
            return []
        return list(self._last_chain.get("records", {}).get("expiryDates", []))

    def status(self) -> ProviderStatus:
        if self._last_error:
            return ProviderStatus.ERROR
        return ProviderStatus.LIVE if self._last_chain is not None else ProviderStatus.CONNECTING

    def status_detail(self) -> str | None:
        return self._last_error

    def get_underlying_ltp(self) -> float:
        if self._last_chain is None:
            self.fetch_option_chain()
        assert self._last_chain is not None
        return float(self._last_chain["records"]["underlyingValue"])

    def stream_ticks(self):
        while True:
            chain = self.fetch_option_chain()
            spot = float(chain["records"]["underlyingValue"])
            ts_raw = chain["records"].get("timestamp", "")
            try:
                ts = datetime.strptime(ts_raw, "%d-%b-%Y %H:%M:%S")
            except ValueError:
                ts = now_ist()
            yield PriceTick(symbol=self.underlying, price=spot, timestamp=ts, provider=self.provider_name, exchange="NSE")
            time.sleep(self.poll_interval)
