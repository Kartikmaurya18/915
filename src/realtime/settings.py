"""Deployment topology, read from the environment on every call.

REALTIME_MODE
    embedded (default)  one process owns the engine AND serves clients. Local development,
                        tests, and small single-instance deployments. No Redis needed.
    gateway             serves clients only; market data arrives from Redis and engine-bound
                        requests go to the ingestor. Run any number of these.
The ingestor is a separate entrypoint (`python -m src.ingestor`); it is never an HTTP server.
"""

from __future__ import annotations

import os


def mode() -> str:
    value = os.getenv("REALTIME_MODE", "embedded").strip().lower()
    if value not in ("embedded", "gateway"):
        raise ValueError(f"REALTIME_MODE must be 'embedded' or 'gateway', got {value!r}")
    return value


def is_gateway() -> bool:
    return mode() == "gateway"
