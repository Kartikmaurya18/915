# Real-time distribution (Phase 2)

How market data gets from the provider to a browser, what happens when parts fail, and how to
run it. "Verified" means exercised by a test or a measurement described here; anything else is
marked as not verified.

## Topology

```
 provider ──► INGESTOR (leader) ──► Redis ──► GATEWAY ──► browsers   (/ws/v1)
              INGESTOR (standby)   lease +      GATEWAY
                    │              pub/sub      (any number)
                    └──────────────────────────► PostgreSQL ◄── GATEWAY (users, trades)
```

| Process | Does | Does NOT |
|---|---|---|
| **Ingestor** (`python -m src.ingestor`) | Holds the provider connection, runs the engine, publishes versioned topic updates, decides paper-trade take-profit/stop-loss exits, answers control-channel requests. Only the **leader** does any of this. | Serve browsers. |
| **Gateway** (`REALTIME_MODE=gateway`) | Authenticates users, serves REST and `/ws/v1`, prices and records paper trades. Holds only a *derived* copy of the latest topic state. | Open a provider connection, tick, or decide exits. |
| **Embedded** (`REALTIME_MODE=embedded`, default) | Both, in one process. Local development, tests, single-instance deployments. No Redis needed. | — |

Run **at least two ingestors** (one leads, the rest are warm standbys) and as many gateways as load requires.
`AUTH_MODE=required` is mandatory for gateways.

## Topics

| Topic | Contents |
|---|---|
| `index:NIFTY` | price, ATM, calculated prices, provider/status, data-quality and staleness |
| `options:NIFTY` | the option chain |
| `candles:NIFTY:1m` / `5m` / `15m` | active candle, completed candles, series |
| `rrg:NIFTY` | relative rotation graph (only when enabled) |

A client subscribes to exactly what its page shows. **No user's paper book is ever a topic**;
per-user state is fetched over authenticated REST.

## Protocol v1 (`/ws/v1`)

See `src/realtime/protocol.py` for the authoritative frame list. In short: the client sends
`auth` (when auth is on), then `subscribe {topics}`. For each topic it receives a `snapshot`, then
`patch` frames. A patch carries `base` — the version it applies to — and the client applies it only
if it holds exactly that version; otherwise it sends `resync` and gets a fresh snapshot.
`heartbeat` frames report `feed_age_ms`, so a client can show "feed down" even when no data flows.

A reference client state machine is `src/realtime/client.py`; port it line-for-line to the browser.

### Ordering and duplicates

Every topic message carries `(epoch, seq)`. `seq` increases within an epoch; `epoch` is the leader's
fencing token and increases on every failover (and never goes backwards, even if Redis loses its data).
Receivers apply a message only if `(epoch, seq)` is strictly greater than what they hold; duplicates and
stale messages are dropped and counted (`rt_updates_total{result="duplicate|out_of_order"}`).

### Slow clients

A client's "queue" is not a queue of messages. Each client holds the set of topics that changed since it
last sent, and the version it last sent. When its socket is ready the gateway sends the **current** state:
a patch if the client is exactly one version behind, a snapshot otherwise. Consequences, all tested:

* a slow client skips intermediate versions instead of accumulating them — memory per client is bounded by
  the number of topics it subscribed to, however fast the feed runs;
* the ingest path never waits on any client (`Hub.apply_update` is a plain function);
* a client that does not accept data for `RT_SEND_TIMEOUT_SECONDS` (default 5) is disconnected.

## Failure behaviour

| Event | What happens | Verified |
|---|---|---|
| Leader process killed | Lease expires; standby acquires it with a higher epoch and starts the engine. Gap ≈ one lease TTL plus start-up. | yes (subprocess kill test) |
| Leader shut down gracefully | Lease released immediately; standby takes over in about half a second. | yes |
| Leader stalls past its lease, then resumes | Redis rejects its stale fencing token; it stops publishing, stops its engine and stands down. It cannot overwrite the new leader's data. | yes (fencing + stale-leader tests, with mutation checks) |
| Redis connection drops | Each gateway reconnects with backoff, re-reads the stored snapshots, and resumes. Clients keep the last state and see `feed_age_ms` grow. | yes |
| Redis loses all data | Gateways keep serving the last state; the next leader's epoch (time-based) is higher, so its data is accepted. | yes |
| Pub/sub message lost | Healed by the next message (each carries the full payload) or by the periodic snapshot reconcile (`RT_RECONCILE_SECONDS`, default 30). | yes |
| No leader at all | Gateways serve stale topic state with a growing `feed_age_ms`; engine-bound endpoints return `503 ingestor_unavailable`; DB-backed features keep working; new paper trades are refused once the feed is older than 60 s. | yes |
| Gateway restarts | The gateway rebuilds its state from Redis snapshots on start and holds no per-user state, so nothing is lost. Clients must reconnect and resubscribe; the reference client (`src/realtime/client.py`) covers the protocol, but reconnect-with-jittered-backoff belongs to the browser WebSocket manager (Phase 3) and is **not yet built or tested**. | server side yes; client side no |
| Database unavailable | `/api/ready` fails; gateways stop receiving traffic from the load balancer. Market data still flows through Redis. | partly (readiness check tested; load-balancer behaviour is deployment-specific) |

### Control channel

Endpoints that need the engine itself (live settings, logs, history, strike candles) go to the leader over
a Redis list (`src/realtime/control.py`). Only whitelisted read/configure methods can be called; the
leader's control thread is the only consumer; callers get `503` if nobody answers within the timeout.
This is an interim bridge: history is served from the ingestor's local store, so it is **not** durable
across ingestor hosts until market history moves to PostgreSQL.

## Operating it

* `docker compose up --build` starts Postgres, Redis, a migration job, two ingestors and two gateways
  (`:8000`, `:8001`). `docker compose stop ingestor1` (or whichever leads) demonstrates failover.
* Metrics: each gateway serves `/metrics` (set `METRICS_TOKEN`); each ingestor serves metrics on
  `INGESTOR_METRICS_PORT` (default 9100). Key series: `rt_ws_clients`, `rt_subscriptions`,
  `rt_fanout_seconds`, `rt_coalesced_total`, `rt_clients_dropped_total`, `rt_snapshots_sent_total{reason}`,
  `rt_updates_total`, `rt_feed_age_seconds`, `rt_redis_up`, `rt_is_leader`, `rt_leader_changes_total`,
  `rt_publish_total{result}`, `rt_control_rpc_seconds`.
* Alert on: `rt_is_leader` summing to 0 for > 2× lease TTL across ingestors; `rt_feed_age_seconds` > 15;
  `rt_redis_up == 0`; `rate(rt_clients_dropped_total{reason="slow_consumer"})` rising;
  `rt_publish_total{result="fenced"}` > 0 (a stale leader existed — investigate why a lease lapsed).
* Redis must be on a private network with a password (and TLS where supported): the control channel trusts
  whoever can write to it.

## Known limits

* In-progress candle state lives in the leader's memory; a failover restarts the current candle on the new leader.
* Redis is a single instance in this setup; Redis itself is a single point of failure for *new* data (existing
  connections keep serving the last state). Use a managed Redis with replication for production.
* The legacy single-blob `/ws` still exists (embedded mode only) for the old UI; it is not offered by gateways.
* The legacy history store is still SQLite on the ingestor's disk (see the control-channel note).
