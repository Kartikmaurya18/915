"""Per-stage latency of the in-process pipeline, measured on the real code path.

    python scripts/bench_pipeline.py --ticks 2000 --greeks calculated
    python scripts/bench_pipeline.py --ticks 2000 --greeks provider      # the baseline, for comparison

    feed adapter -> Greeks/IV -> ATM, candles, paper marks -> snapshot -> topic split + change detection
                 -> patch + JSON encoding (what a gateway does once per update, shared by all clients)

Measures compute time only, on whatever machine runs it, with the MOCK feed. It says nothing about network
delay or about a real provider's feed latency: those need a real provider and a real deployment
(scripts/loadtest_ws.py measures delivery to clients). Nothing here is a claim about any other product.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.engine import MarketEngine  # noqa: E402
from src.models import PriceTick  # noqa: E402
from src.realtime import protocol  # noqa: E402
from src.realtime.patch import diff  # noqa: E402
from src.realtime.publisher import TopicPublisher  # noqa: E402
from src.timeutil import now_ist  # noqa: E402


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def summarise(name: str, samples_ms: list[float]) -> dict:
    return {
        "stage": name,
        "p50_ms": round(statistics.median(samples_ms), 3),
        "p95_ms": round(pct(samples_ms, 0.95), 3),
        "p99_ms": round(pct(samples_ms, 0.99), 3),
        "max_ms": round(max(samples_ms), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ticks", type=int, default=2000)
    parser.add_argument("--greeks", choices=["provider", "calculated"], default="calculated")
    parser.add_argument("--json", action="store_true", help="print machine-readable output only")
    args = parser.parse_args()

    engine = MarketEngine(
        {
            "underlying": "NIFTY",
            "market_data_provider": "mock",
            "data_source": "mock",
            "poll_interval_seconds": 1,
            "candle_intervals": [1, 5, 15],
            "price_calculator": "mock",
            "greeks_source": args.greeks,
            "persistence": {"enabled": False},
            "log_level": "ERROR",
        }
    )
    publisher = TopicPublisher(lambda _updates: None, engine.underlying, epoch=1)
    fetch, enrich, process, publish, encode = [], [], [], [], []
    patch_bytes: list[int] = []
    snapshot_bytes = 0
    previous: dict[str, object] = {}

    try:
        for _ in range(args.ticks):
            tick = PriceTick(symbol="NIFTY", price=24850.0, timestamp=now_ist(), provider="MOCK")

            t0 = time.perf_counter()
            chain = engine._source.get_option_chain()
            t1 = time.perf_counter()
            engine._greeks.enrich(chain)
            t2 = time.perf_counter()
            fetch.append((t1 - t0) * 1000)
            enrich.append((t2 - t1) * 1000)

            t3 = time.perf_counter()
            snapshot = engine._process_tick_data(tick)  # includes its own fetch + enrich: the whole engine step
            t4 = time.perf_counter()
            updates = publisher.build(snapshot)
            t5 = time.perf_counter()
            process.append((t4 - t3) * 1000)
            publish.append((t5 - t4) * 1000)

            t6 = time.perf_counter()
            for u in updates:  # what the gateway hub does once per update, then shares with every client
                old = previous.get(u.topic)
                node = diff(old, u.payload) if old is not None else None
                frame = (
                    protocol.patch_message(u.topic, u.epoch, u.seq, u.seq - 1, u.ts, u.pub, node)
                    if node is not None
                    else protocol.snapshot_message(u.topic, u.epoch, u.seq, u.ts, u.pub, u.payload)
                )
                if u.topic.startswith("options:"):
                    patch_bytes.append(len(frame.encode()))
                    if old is None:
                        snapshot_bytes = len(frame.encode())
                previous[u.topic] = u.payload
            encode.append((time.perf_counter() - t6) * 1000)
    finally:
        engine.close()

    rows = [
        summarise("feed adapter: fetch + parse chain", fetch),
        summarise(f"greeks engine ({args.greeks})", enrich),
        summarise("engine step: whole tick (fetch, greeks, ATM, candles, paper, snapshot)", process),
        summarise("publisher: topic split + change detection", publish),
        summarise("hub: patch + JSON encode (once per update, shared by all clients)", encode),
    ]
    first = patch_bytes[1:] or [0]
    result = {
        "ticks": args.ticks,
        "greeks_source": args.greeks,
        "stages": rows,
        "options_topic": {
            "snapshot_bytes": snapshot_bytes,
            "patch_bytes_p50": int(statistics.median(first)),
            "patch_bytes_p95": int(pct(first, 0.95)),
        },
        "note": "compute only, MOCK feed, this machine; excludes network and any real provider",
    }
    if args.json:
        print(json.dumps(result))
        return 0
    print(f"{args.ticks} ticks, greeks_source={args.greeks}  (compute only, MOCK feed)\n")
    print(f"{'stage':<82}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}  (ms)")
    for r in rows:
        print(f"{r['stage']:<82}{r['p50_ms']:>9}{r['p95_ms']:>9}{r['p99_ms']:>9}{r['max_ms']:>9}")
    o = result["options_topic"]
    print(f"\noptions topic: snapshot {o['snapshot_bytes']} B; patch p50 {o['patch_bytes_p50']} B, p95 {o['patch_bytes_p95']} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
