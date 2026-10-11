"""
Deterministic MOCK Replay/Test Mode.

Unlike `MockDataSource` (which uses the process-global `random` module and is
therefore non-reproducible run-to-run), `ReplayDataSource` walks a fixed,
named sequence of underlying-price deltas with its own seeded RNG for the
option-chain "noise" (OI/volume jitter). Same sequence + same speed always
produces the same tick-by-tick path — useful for demos, UI testing, and
reproducing a specific market scenario (trend, chop, gap) on demand.

Never touches a real broker; this is pure simulation, same as MockDataSource.
"""

from __future__ import annotations

import math
import random
import time

from src.data_sources.base import DataSource
from src.data_sources.mock import MOCK_BASE_SPOT
from src.instruments import get_underlying_spec
from src.models import PriceTick
from src.option_chain.expiry import resolve_mock_expiry
from src.timeutil import now_ist

# Each sequence is a list of cumulative deltas (points) applied to `base_spot`.
# Deterministic and hand-authored so the resulting spot path is reproducible
# and recognizable as a scenario (not just "random but seeded").
REPLAY_SEQUENCES: dict[str, list[float]] = {
    "trend_up": [0, 8, 14, 22, 28, 35, 40, 48, 55, 60, 68, 75, 80, 88, 95, 100, 108, 115, 120, 128],
    "trend_down": [0, -8, -14, -22, -28, -35, -40, -48, -55, -60, -68, -75, -80, -88, -95, -100, -108, -115, -120, -128],
    "choppy_range": [0, 12, -6, 18, -10, 22, -14, 16, -8, 20, -12, 14, -18, 10, -6, 16, -10, 8, -14, 6],
    "gap_and_reversal": [0, 0, 60, 65, 70, 68, 55, 30, 5, -20, -35, -30, -15, 0, 10, 18, 12, 5, 10, 15],
}
DEFAULT_SEQUENCE = "trend_up"


class ReplayDataSource(DataSource):
    """
    Controllable, deterministic tick sequence for demos and test mode.

    - `sequence`: name from REPLAY_SEQUENCES (falls back to DEFAULT_SEQUENCE
      if unknown, never raises, so a stale saved setting can't break startup).
    - `speed`: playback multiplier — 2.0 plays ticks twice as fast (half the
      sleep), 0.5 plays half speed. Clamped to a sane [0.1, 20] range.
    - The sequence loops when exhausted so replay mode can run indefinitely.
    """

    provider_name = "REPLAY"
    is_simulated = True

    def __init__(
        self,
        underlying: str = "NIFTY",
        poll_interval: float = 1.0,
        timezone: str = "Asia/Kolkata",
        sequence: str = DEFAULT_SEQUENCE,
        speed: float = 1.0,
        base_spot: float | None = None,
        seed: int = 42,
        expiry: str | None = None,
    ):
        self.underlying = underlying.upper()
        self.poll_interval = poll_interval
        self.timezone = timezone
        self.spec = get_underlying_spec(self.underlying)
        if base_spot is None:
            base_spot = MOCK_BASE_SPOT.get(self.underlying, 24_850.0)
        self.sequence_name = sequence if sequence in REPLAY_SEQUENCES else DEFAULT_SEQUENCE
        self.speed = min(max(float(speed), 0.1), 20.0)
        self.base_spot = base_spot
        self._deltas = REPLAY_SEQUENCES[self.sequence_name]
        self._rng = random.Random(seed)
        self._index = 0
        self._spot = base_spot
        # Same configurable-or-auto-computed expiry as MockDataSource (see
        # resolve_mock_expiry) — kept in sync so Replay/Test Mode never shows
        # a different (or stale) expiry than live MOCK mode.
        self.expiry = resolve_mock_expiry(expiry, timezone)

    @property
    def effective_sleep(self) -> float:
        return max(0.01, self.poll_interval / self.speed)

    def _advance(self) -> float:
        delta = self._deltas[self._index % len(self._deltas)]
        self._index += 1
        self._spot = round(self.base_spot + delta, 2)
        return self._spot

    def fetch_option_chain(self) -> dict:
        spot = self._spot
        step = self.spec.strike_step
        atm_strike = round(spot / step) * step
        strikes = []
        for i in range(-5, 6):
            strike = atm_strike + i * step
            distance = abs(strike - spot)
            call_ltp = max(5, 200 - distance * 0.8 + self._rng.uniform(-3, 3))
            put_ltp = max(5, 200 - distance * 0.8 + self._rng.uniform(-3, 3))

            signed_distance = strike - spot
            call_delta = round(1.0 / (1.0 + math.exp(signed_distance / (2 * step))), 4)
            put_delta = round(call_delta - 1.0, 4)

            strikes.append(
                {
                    "strikePrice": strike,
                    "CE": {
                        "lastPrice": round(call_ltp, 2),
                        "openInterest": self._rng.randint(10000, 500000),
                        "totalTradedVolume": self._rng.randint(1000, 50000),
                        "delta": call_delta,
                    },
                    "PE": {
                        "lastPrice": round(put_ltp, 2),
                        "openInterest": self._rng.randint(10000, 500000),
                        "totalTradedVolume": self._rng.randint(1000, 50000),
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
        return float(self._spot)

    def stream_ticks(self):
        while True:
            spot = self._advance()
            now = now_ist(self.timezone)
            yield PriceTick(
                symbol=self.underlying,
                price=spot,
                timestamp=now,
                provider=self.provider_name,
                exchange=self.spec.exchange,
            )
            time.sleep(self.effective_sleep)
