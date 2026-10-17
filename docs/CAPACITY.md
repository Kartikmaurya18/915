# Capacity: what a gateway can serve

Measured 2026-10-17 with [scripts/loadtest_ws.py](../scripts/loadtest_ws.py). **These are laptop numbers with the load generator on the same
machine. Treat them as a floor and as a way to compare changes, not as a promise for production hardware.** Read the caveats.

## What was measured

| | |
|---|---|
| Machine | AMD Ryzen 5 5500U (6 cores / 12 threads), Windows 11, about 1 GB of free RAM; other containers (a Kubernetes control plane using 35–240 % CPU) running during the Docker runs |
| Native runs | **one** `uvicorn` process on Windows, embedded mode, `AUTH_MODE=disabled`, mock feed |
| Docker runs | the compose topology: 2 gateways, 2 ingestors, Redis, PostgreSQL (`AUTH_MODE=required`), Docker Desktop (WSL2) |
| Feed | **4 ticks/s** (fast; stress) and **1 tick/s** (nearer a real provider's option-chain rate) |
| Clients | 60 % option chain (`index` + `options`), 25 % chart (`index` + `candles`), 15 % dashboard (`index`) |
| Duration | 30 s of steady traffic per run |

Latency is `receive time − the ingestor's publish time` on one clock, so it includes the generator's own scheduling delay (it shares the CPU).

## Two changes that mattered, found by measuring

1. **WebSocket compression was costing a quarter of the CPU.** By default uvicorn compresses every outgoing message separately for every client (permessage-deflate).
   Per-client traffic is only ~9 KB/s, so the bandwidth saved is negligible. The image now starts with `--ws-per-message-deflate false`.
2. **Batching.** A client with several topics used to receive one WebSocket message per topic per tick. Clients can now opt in (`subscribe` with `batch: true`; the web app does)
   and get all of a tick's topic updates in one message, after a 4 ms collection window (`RT_BATCH_WINDOW_MS`). Clients that do not opt in behave exactly as before.

Native, 500 clients, 4 ticks/s, one process. "Before" is the average of two runs; the other rows are single runs. The machine was busier than during the first capacity test, so absolute values are worse than the 336 ms p99 first seen at this size; compare rows with each other, not with that figure:

| Configuration | Messages/s | Latency p50 / p95 / p99 | Server CPU | Catch-up snapshots |
|---|---:|---|---:|---:|
| Before (compression on, no batching) | 3,650 | 187 / 613 / 832 ms | 73 % of a core | ~14,500 |
| Compression off | 3,870 | 124 / 473 / 745 ms | 56 % | 3,559 |
| Compression off **and** batching | 2,156 | **86 / 292 / 621 ms** | 54 % | 5,725 |

## Two gateways in Docker (the production shape), before and after the two changes

Clients are split evenly across the two gateways. "Connected" is out of the clients requested; the rest timed out connecting.

| Feed | Clients | Before: connected, latency p50 / p99, gateway CPU | After: connected, latency p50 / p99, gateway CPU |
|---|---:|---|---|
| 1 tick/s | 500 | 500, 160 ms / 1.0 s, 52–85 % | 500, **100 ms / 0.65–0.95 s, 14 %** |
| 1 tick/s | 1,000 | 1,000, 460–590 ms / 2.7–3.7 s, ~100 % | 1,000, **270 ms / 1.4–2.6 s, ~42 %** |
| 1 tick/s | 1,500 | ~1,250 (250 refused/timed out), 630–770 ms / 3.8–8 s, ~100 % | **1,500**, 320–375 ms / 2.4–7.7 s, 100–165 % |
| 4 ticks/s | 500 | 500, 190–210 ms / 0.65–0.95 s, ~115 % | 500, **100 ms / 0.4–0.45 s**, 45–91 % |
| 4 ticks/s | 1,000 | ~590 connected, 510 ms / 1.7–1.8 s, ~104 % | ~890 connected, 320 ms / 1.8 s (11 s on one gateway), ~105 % |

No client ever had to resync in any run.

## What this says

* **At a 1 tick/s feed, two containerized gateways on a busy laptop carried 1,500 simultaneous clients** (all connected, median latency a few hundred ms, p95 about 1 s),
  with a heavy tail (p99 several seconds) that the shared CPU and Docker Desktop's networking explain at least in part.
  **1,000 users therefore looks achievable with two gateways at a realistic feed rate, and comfortable with three.** That is evidence, not proof: it was not measured on
  dedicated Linux hardware with real network paths.
* **At a 4 tick/s feed, 1,000 clients is beyond two gateways here** (about 890 connected, tails of seconds). If your real feed is that fast, plan more gateways or lower the rate.
* A gateway is one single-threaded Python process; it saturates one core. Scaling is by adding replicas, which are stateless.
* The **tail latency** (p99) is dominated by a tick being delivered to hundreds of sockets in sequence: the last client waits for the others. Median is the better guide to
  typical experience; watch p95 in production (`rt_fanout_seconds`, alert `SlowFanOut`).
* The generator and server shared one machine's CPU in every run, which inflates every figure.

## Caveats

* Laptop, shared CPU, other containers running; Docker Desktop's network path adds overhead a real Linux host does not have. Native Windows and Docker numbers differ by a large factor
  for the same code, which is itself a warning not to extrapolate.
* Mock data, no real network, no slow mobile clients except in the dedicated `--slow-fraction` mode (slow clients are disconnected after 5 s without reading and never delay others).
* One generator token was used for all clients (the per-user connection cap was raised for the test gateways only).
* Not measured: more than two gateways, a real load balancer, TLS termination, real provider ticks.

## Reproduce

```bash
# native, one process
AUTH_MODE=disabled MARKET_DATA_PROVIDER=mock POLL_INTERVAL_SECONDS=0.25 METRICS_TOKEN=loadtest \
  python -m uvicorn src.api.server:app --port 8020 --ws-per-message-deflate false --log-level warning
python scripts/loadtest_ws.py --url ws://127.0.0.1:8020 --clients 500 --duration 30 --ramp 12 --processes 4 --batch --json

# the production shape
docker compose up -d --build        # 2 ingestors, 2 gateways, Redis, PostgreSQL
# raise WS_MAX_CONNECTIONS_PER_USER on the gateways (test only) and use one token for the generator:
python scripts/loadtest_ws.py --url ws://127.0.0.1:8000 --clients 500 --token "$TOKEN" --batch --duration 30 --ramp 20
python scripts/loadtest_ws.py --url ws://127.0.0.1:8001 --clients 500 --token "$TOKEN" --batch --duration 30 --ramp 20
```
Run the generator on a **separate machine** if you can. `--slow-fraction 0.1` adds slow readers; omit `--batch` to see the old per-topic behaviour.

## Sizing guidance (until measured on your hardware)

Start with **three gateways for 1,000 concurrent users** (two is enough at a 1 tick/s feed on this evidence, three adds headroom and availability during a deploy),
add one per ~400–500 users beyond that, and re-measure on the real infrastructure and the real feed rate before committing to a budget. Each gateway holds one
process's worth of sockets; set `WS_MAX_CONNECTIONS_TOTAL` to about 800 so an overloaded gateway refuses new sockets instead of degrading everyone.
