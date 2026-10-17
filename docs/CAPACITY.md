# Capacity: what one gateway process can serve

Measured 2026-10-17 with [scripts/loadtest_ws.py](../scripts/loadtest_ws.py). These numbers are a floor,
not a promise: read the caveats before using them to size anything.

## Setup

| | |
|---|---|
| Machine | AMD Ryzen 5 5500U (6 cores / 12 threads), Windows 11, other containers stopped |
| Server | **one** `uvicorn src.api.server:app` process, embedded mode, `AUTH_MODE=disabled`, mock feed at **4 ticks/s** |
| Load generator | 3–4 Python processes on **the same machine**, so it competes with the server for CPU |
| Clients | realistic mix: 60% option chain (`index` + `options`), 25% chart (`index` + `candles`), 15% dashboard (`index`) |
| Duration | 30 s of steady traffic per run, after a ramp |

Latency is `receive time − frame.pub` (the ingestor's publish time) on one clock, so it includes the generator's own
scheduling delay. It over-states what a user on a separate machine would see.

## Results

| Clients | Connected | Frames/s | Latency p50 / p95 / p99 | Server CPU | Full snapshots sent | Resyncs |
|---:|---:|---:|---|---:|---:|---:|
| 100 | 100 | 727 | fan-out p50 25 ms (end-to-end latency not captured) | – | 0 | 0 |
| 300 | 300 | 2,379 | 91 / 141 / 200 ms | – | 555 | 0 |
| 500 | 500 | 4,362 | 147 / 248 / 336 ms | **73 % of one core** | 925 | 0 |
| 600 | 600 | 4,792 | 199 / 321 / 383 ms | – | 1,110 | 0 |
| 1,000 | **853** (113 refused, 34 timed out) | 8,298 | 343 / 542 / 633 ms | **85 % of one core** | 22,932 | 0 |

Memory grew from about 100 MB (100 clients) to about 200 MB (1,000 clients). Each client costs roughly 0.1 MB.
Average patch frame: ~1.15 KB. Average full snapshot: ~2.6 KB. Per-client bandwidth: ~9 KB/s at 4 ticks/s.
No client ever had to resync (0 resyncs in the 300, 500, 600 and 1,000-client runs). In each run 15–38 clients parsed every frame fully and applied every patch cleanly; the generator counts resyncs but does not separately assert state equality (that is covered by the live end-to-end tests).

## What this says

* **One gateway process is comfortable up to roughly 500 concurrent clients** at a 4 Hz feed (p99 under about 340 ms on a
  loaded laptop) and starts to fall behind between 600 and 800: latency approaches the 250 ms tick period, and the hub
  responds as designed by skipping versions and sending full snapshots to clients that cannot keep up (22,932 snapshots
  at 1,000 clients against 925 at 500). Slow clients degrade; they do not stall anyone else, and nobody is disconnected.
* **1,000+ users therefore needs at least two gateway replicas**, three for headroom. Gateways are stateless and take
  their data from Redis, so this is a scaling knob, not a redesign. *Measured here: one process. Not measured here:
  several gateways behind a load balancer reading from Redis* (no Redis was available on this machine).
* Rate matters. The feed was 4 ticks/s, which is faster than the 1–2 s a real provider normally delivers for an option
  chain; at 1 tick/s the same process should carry several times more clients. Measure at your real rate.
* The 1,000-client run's refused connections are the operating system's accept queue overflowing while the server was
  busy sending, not a limit in the application. A load balancer in front, and a larger `--backlog`, remove it.

## Caveats (why these are a floor)

* The generator ran on the same CPU as the server and was itself a heavy user of it.
* Windows' default asyncio loop is slower than Linux with `uvloop`; a production Linux container typically does noticeably
  better. Not measured here.
* Mock data and `AUTH_MODE=disabled`. With auth on, each connection adds a token check once at connect, nothing per frame.
* Serialization is shared (each topic update is encoded once for all subscribers); the per-client cost is the socket write.
* No real network: no latency, loss or slow mobile clients except in the dedicated `--slow-fraction` mode (tested in
  Phase 2: slow clients are disconnected after 5 s without reading and never delay others).

## Reproduce

```bash
# server (any machine)
AUTH_MODE=disabled MARKET_DATA_PROVIDER=mock POLL_INTERVAL_SECONDS=0.25 METRICS_TOKEN=loadtest \
  python -m uvicorn src.api.server:app --port 8020 --log-level warning

# load (separate machine if you can)
python scripts/loadtest_ws.py --url ws://127.0.0.1:8020 --clients 500 --duration 30 --ramp 12 --processes 4 --json
```
Add `--register 50` against an `AUTH_MODE=required` server (mind the 10-registrations-per-hour-per-IP limit), and
`--slow-fraction 0.1` to include slow readers. For the Redis topology, run the compose stack
(`docker compose up --build`) and point the generator at both gateways.

## Sizing rule of thumb

`gateways ≈ ceil(concurrent users / 400)` at a 4 Hz feed on a comparable core, with a minimum of two for availability.
Re-measure on your target hardware and real feed rate before committing to a number.
