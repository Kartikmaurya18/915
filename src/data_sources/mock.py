import math
import random
import time

from src.data_sources.base import DataSource
from src.instruments import get_underlying_spec
from src.models import PriceTick
from src.option_chain.expiry import resolve_mock_expiry
from src.timeutil import now_ist

# Simulation starting spot per underlying. MOCK-only: real providers report
# the actual market level. Unlisted underlyings start from NIFTY's.
MOCK_BASE_SPOT = {
    "NIFTY": 24_850.0,
    "BANKNIFTY": 55_000.0,
    "FINNIFTY": 26_000.0,
    "SENSEX": 81_500.0,
    "BANKEX": 62_000.0,
}


class MockDataSource(DataSource):
    """
    Simulated market data for development and demos.
    Generates index-like prices and an option chain for any underlying in
    src/instruments.py (strike spacing and exchange come from there).
    """

    provider_name = "MOCK"
    is_simulated = True

    def __init__(
        self,
        underlying: str = "NIFTY",
        poll_interval: float = 5.0,
        timezone: str = "Asia/Kolkata",
        expiry: str | None = None,
    ):
        self.underlying = underlying.upper()
        self.poll_interval = poll_interval
        self.timezone = timezone
        self.spec = get_underlying_spec(self.underlying)
        self._spot = MOCK_BASE_SPOT.get(self.underlying, 24_850.0)
        self._drift = 0.0
        # `expiry` (config.yaml's `expiry` key / `EXPIRY` env var) pins the
        # date reported below; leave it unset (None) to auto-compute the next
        # weekly expiry instead of a hard-coded, ever-staler date — see
        # resolve_mock_expiry(). Resolved once per instance so it stays
        # consistent (Option Chain / Paper Trading / Straddle Chart all read
        # the same value) for the life of this data source.
        self.expiry = resolve_mock_expiry(expiry, timezone)

    def fetch_option_chain(self) -> dict:
        self._spot += random.uniform(-15, 15)
        spot = round(self._spot, 2)
        step = self.spec.strike_step
        atm_strike = round(spot / step) * step
        strikes = []
        for i in range(-5, 6):
            strike = atm_strike + i * step
            distance = abs(strike - spot)
            call_ltp = max(5, 200 - distance * 0.8 + random.uniform(-5, 5))
            put_ltp = max(5, 200 - distance * 0.8 + random.uniform(-5, 5))

            # Mock Greeks: a logistic curve centered on spot gives Call Delta ~0.5
            # (and Put Delta ~-0.5) right at the money, decaying toward 0/1 as the
            # strike moves ITM/OTM — good enough to exercise Delta-based ATM
            # selection without a full pricing model.
            signed_distance = strike - spot
            # Curve width scales with strike spacing (100 for NIFTY's 50-pt step).
            call_delta = round(1.0 / (1.0 + math.exp(signed_distance / (2 * step))), 4)
            put_delta = round(call_delta - 1.0, 4)

            strikes.append(
                {
                    "strikePrice": strike,
                    "CE": {
                        "lastPrice": round(call_ltp, 2),
                        "openInterest": random.randint(10000, 500000),
                        "totalTradedVolume": random.randint(1000, 50000),
                        "delta": call_delta,
                    },
                    "PE": {
                        "lastPrice": round(put_ltp, 2),
                        "openInterest": random.randint(10000, 500000),
                        "totalTradedVolume": random.randint(1000, 50000),
                        "delta": put_delta,
                    },
                }
            )

        now = now_ist(self.timezone)
        return {
            "records": {
                "underlyingValue": spot,
                "expiryDates": [self.expiry],
                "timestamp": now.strftime("%d-%b-%Y %H:%M:%S"),
                "data": strikes,
            }
        }

    def list_expiries(self) -> list[str]:
        return [self.expiry]

    def get_underlying_ltp(self) -> float:
        return float(self.fetch_option_chain()["records"]["underlyingValue"])

    def stream_ticks(self):
        while True:
            chain = self.fetch_option_chain()
            spot = float(chain["records"]["underlyingValue"])
            now = now_ist(self.timezone)
            yield PriceTick(
                symbol=self.underlying,
                price=spot,
                timestamp=now,
                provider=self.provider_name,
                exchange=self.spec.exchange,
            )
            time.sleep(self.poll_interval)
