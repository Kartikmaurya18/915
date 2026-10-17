import math
import random
import time

from src.data_sources.base import DataSource
from src.instruments import get_underlying_spec
from src.models import PriceTick
from src.option_chain import analytics
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


# Simulation parameters. MOCK-only, like the starting spots above.
MOCK_RATE = 0.065
MOCK_DIVIDEND = 0.012
MOCK_BASE_VOL = {"NIFTY": 0.13, "BANKNIFTY": 0.16, "FINNIFTY": 0.15, "SENSEX": 0.13, "BANKEX": 0.16}


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
        self._base_vol = MOCK_BASE_VOL.get(self.underlying, 0.14)
        self._oi: dict[tuple[float, str], list[int]] = {}
        self._volume: dict[tuple[float, str], int] = {}
        # `expiry` (config.yaml's `expiry` key / `EXPIRY` env var) pins the
        # date reported below; leave it unset (None) to auto-compute the next
        # weekly expiry instead of a hard-coded, ever-staler date — see
        # resolve_mock_expiry(). Resolved once per instance so it stays
        # consistent (Option Chain / Paper Trading / Straddle Chart all read
        # the same value) for the life of this data source.
        self.expiry = resolve_mock_expiry(expiry, timezone)

    def _years_to_expiry(self, now) -> float:
        # Floored so the chain keeps a sane shape on expiry day after the close.
        return max(analytics.years_to_expiry(self.expiry, now) or 0.0, 1e-4)

    def _vol(self, strike: float, forward: float) -> float:
        """A smile with the usual equity-index downside skew, around the underlying's base volatility."""
        x = math.log(strike / forward)
        return max(0.05, self._base_vol - 0.9 * x + 8.0 * x * x)

    def _open_interest(self, strike: float, spot: float, side: str) -> tuple[int, int]:
        """(current, previous-session) OI. Concentrated near the money, drifting slowly - not re-rolled every tick."""
        key = (strike, side)
        state = self._oi.get(key)
        if state is None:
            rng = random.Random(f"{self.underlying}|{strike}|{side}")
            step = self.spec.strike_step
            base = int(450_000 * math.exp(-(((strike - spot) / (6 * step)) ** 2)) * rng.uniform(0.6, 1.4)) + rng.randint(8_000, 30_000)
            previous = max(1_000, int(base * rng.uniform(0.85, 1.05)))
            state = self._oi[key] = [base, previous]
        state[0] = max(1_000, state[0] + random.randint(-1_500, 1_800))
        return state[0], state[1]

    @staticmethod
    def _tick(value: float) -> float:
        return round(max(0.05, round(value / 0.05) * 0.05), 2)

    def fetch_option_chain(self) -> dict:
        scale = self._spot / 24_850.0
        self._spot += random.uniform(-15, 15) * scale
        spot = round(self._spot, 2)
        step = self.spec.strike_step
        atm_strike = round(spot / step) * step
        now = now_ist(self.timezone)
        t = self._years_to_expiry(now)
        forward = spot * math.exp((MOCK_RATE - MOCK_DIVIDEND) * t)
        strikes = []
        for i in range(-5, 6):
            strike = atm_strike + i * step
            vol = self._vol(strike, forward)
            row = {"strikePrice": strike}
            for side, opt in (("CE", "CE"), ("PE", "PE")):
                theoretical = analytics.price(opt, spot, strike, t, MOCK_RATE, MOCK_DIVIDEND, vol)
                ltp = self._tick(theoretical + random.uniform(-0.15, 0.15) * max(1.0, theoretical) ** 0.5)
                half_spread = max(0.05, round(ltp * 0.004 / 0.05) * 0.05)
                oi, previous_oi = self._open_interest(strike, spot, side)
                volume = self._volume.get((strike, side), int(oi * 0.4)) + random.randint(0, 900)
                self._volume[(strike, side)] = volume
                row[side] = {
                    "lastPrice": ltp,
                    "bidprice": self._tick(ltp - half_spread),
                    "askPrice": self._tick(ltp + half_spread),
                    "openInterest": oi,
                    "previousOpenInterest": previous_oi,
                    "totalTradedVolume": volume,
                    # No delta / IV: a simulated feed has no exchange Greeks. With greeks_source=calculated the
                    # engine derives them from these prices exactly as it would for a real chain.
                }
            strikes.append(row)

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
