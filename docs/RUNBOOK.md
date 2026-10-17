# Runbook

What to do when something is wrong. Each section is linked from an alert in
[deploy/prometheus/alerts.yml](../deploy/prometheus/alerts.yml). Background on the moving parts is in
[REALTIME.md](REALTIME.md) and [OPERATIONS.md](OPERATIONS.md); capacity numbers are in [CAPACITY.md](CAPACITY.md).

Throughout: **a gateway never connects to the data provider; only the ingestor that holds the Redis lease does.** Users keep
their last data when the feed stops, so most feed problems are degraded, not down.

## Quick triage

| Symptom | Look at first |
|---|---|
| Prices frozen, "Feed stale" pill in the app | `rt_feed_age_seconds` on gateways, then `rt_is_leader` on ingestors |
| Nobody can load history, settings or straddle candles (HTTP 503 `ingestor_unavailable`) | No leader (below); these go through the leader |
| Everything works but is slow | `rt_fanout_seconds` p95 on gateways (saturation), then CPU |
| Users cannot sign in | Database reachability (`/api/ready`), then rate limiting |
| Trades fail with 400 "Market data is stale" | The feed, not the trading code: the quote is older than 60 s |

`GET /api/health` is liveness only. `GET /api/ready` checks dependencies (Redis in gateway mode, database schema) and reports
`feed` separately without failing readiness, so a data outage does not make a load balancer pull every gateway.

## No ingestor leader

*Alert: NoIngestorLeader, ControlChannelFailing.* No market data is published and the control channel has no one to answer.

1. Are ingestors running? `up{job="ingestor"}`; container/platform status. Start them if not. Two are recommended.
2. Can they reach Redis? Ingestor logs: `redis` errors. If Redis is down, go to [Redis down](#redis-down).
3. Is a stale lease blocking takeover? A crashed leader's lease expires after `INGESTOR_LEASE_TTL_MS` (default 6 s, so
   failover takes about one TTL). If it is still stuck after that, inspect `redis-cli GET rt:leader:holder`.
4. Standbys are healthy but nobody leads: look at the ingestor logs for why `try_acquire` returns nothing. As a last resort
   `redis-cli DEL rt:leader:holder` lets a standby take over; the fencing token still increases, so this is safe.
5. A leader exists but publishes nothing: the data provider is failing. Check the ingestor log for provider errors and the
   `/api/ready` or snapshot `status`. Mock data keeps flowing; a real provider that cannot start reports `ERROR`, never fake data.

After recovery, gateways catch up from `rt:snap:*` within one reconcile interval (30 s at most) without clients doing anything.

## Multiple leaders

*Alert: MultipleIngestorLeaders.* Fencing should make this impossible, so it is a defect, not an ops problem.

1. Stop the ingestor that most recently became leader (`rt_leader_changes_total` shows which).
2. Save both ingestors' logs and `redis-cli MGET rt:leader:holder rt:leader:fence rt:leader:counter`.
3. Do not restart the stopped one until the cause is understood. Clients are protected meanwhile: every message carries its
   epoch and a gateway ignores anything older than what it holds.

## Ingestor flapping

*Alert: IngestorFlapping.* Leadership moves repeatedly.

* Usually Redis latency or an overloaded host missing lease renewals. Check Redis `slowlog` and ingestor CPU.
* Raise `INGESTOR_LEASE_TTL_MS` (for example 6000 to 10000). The cost is a slower failover after a real crash.
* A leader that stalls past its lease is fenced and stands down by design; each flap restarts the *current* candle's
  in-memory state from the last published candle, so a few seconds of intra-candle detail can be lost.

## Feed stale

*Alert: MarketFeedStale.* Gateways have not received any market update for over 15 s.

1. During market hours only. The mock/replay source and a closed market also look quiet; check the calendar first.
2. Is there a leader? If not, [No ingestor leader](#no-ingestor-leader).
3. Is Redis connected on the gateway (`rt_redis_up`)? If not, [Redis down](#redis-down).
4. Leader exists, Redis is up, gateways still stale: the provider is not producing ticks, or the engine rejected them
   (`data_quality.rejected_tick_count` in the Data Quality page, `is_stale`).
5. Communicate: the app already shows "Feed stale"; new paper orders are refused after 60 s of stale data so no one trades on old prices.

## Redis down

*Alert: RedisDisconnectedFromGateway.*

What keeps working: connected users keep the last data and their sessions; REST endpoints that only use the database
(sign-in, paper trading reads of positions) keep working; login rate limits fall back to per-process counters (the
`RateLimiterUsingFallback` alert).
What stops: live updates, history/settings/straddle (control channel), new orders (they need a fresh quote from the hub, which
goes stale after 60 s), the exit worker is unaffected only if the leader still has Redis, but a leader that cannot confirm its
lease steps down after about 0.8 of a TTL, so automatic take-profit and stop-loss exits pause too.

1. Restore Redis. It holds no data worth keeping: `--save ""` and no AOF are intended.
2. Everything reconnects with backoff (0.25 s up to 10 s) and re-reads snapshots by itself. No restart is needed.
3. If Redis came back empty, nothing is lost: the fencing counter takes `max(counter, now in ms)` so epochs never go backwards.

## Gateway saturated

*Alerts: SlowFanOut, ManySnapshotsInsteadOfPatches, ManyClientsDropped.*

A gateway is a single asyncio process; about 500 clients at a 4 Hz feed is its comfortable ceiling (see CAPACITY.md).

1. Add gateway replicas behind the load balancer. They are stateless; no coordination is needed.
2. If you cannot, lower the tick rate (`POLL_INTERVAL_SECONDS`), which divides the per-client traffic.
3. Slow clients are disconnected after 5 s without reading; that is protection, not a fault.
4. `rt_clients_dropped_total{reason="server_at_capacity"}` means `WS_MAX_CONNECTIONS_TOTAL` was hit on that gateway; raise it only
   if CPU allows, otherwise add replicas.

## Database down

*`/api/ready` returns 503 with `database: unreachable`.*

Sign-in, registration, paper trading and history of trades fail with 5xx; market data to connected users continues (it does
not touch the database). The market-history writer logs errors and drops those writes rather than blocking the feed, so a
database outage leaves a **gap in stored history** for its duration (live users are unaffected).

1. Restore the database (provider status, connection limits, credentials). Pools reconnect on their own (`pool_pre_ping`).
2. After a long outage check `python -m src.db.maintenance stats` for the history range.
3. If the schema is reported outdated after a restore, run `alembic upgrade head` once as a release step.

## Authentication abuse

* Repeated failed logins for one account lock that account's login for 5 minutes (`429`, `Retry-After`). Per-IP limits are 20
  failures per 5 minutes and 10 registrations per hour. They are shared through Redis, so they hold across gateways.
* **Make sure `TRUSTED_PROXY_HOPS` matches your proxies.** Wrong, and every user appears to come from the load balancer's
  address and shares one limit, or the header is trusted when it should not be and the limit can be dodged.
* To sign everyone out: rotate `JWT_SECRET` (access tokens die at once) and `DELETE FROM refresh_tokens`.
* To remove one user's sessions: set `users.is_active = false`; refresh is refused immediately and access tokens expire within 15 minutes.
* Close registration: `REGISTRATION_OPEN=false` and restart gateways.

## Deploy and rollback

Order: **migrate once, then ingestors, then gateways.**

1. `alembic upgrade head` as its own step (never from every replica's entrypoint). Migrations are written to be backward
   compatible with the previous release.
2. Roll ingestors one at a time. The standby takes over within about one lease TTL; the leader hands over immediately on a
   clean stop (`SIGTERM` releases the lease).
3. Roll gateways one at a time behind the load balancer; connected sockets reconnect and resubscribe automatically.
4. Rollback: redeploy the previous image. If a migration must be undone, `alembic downgrade -1` only if that migration's
   downgrade has been reviewed. **Never** downgrade a migration that drops data without a restore plan.
5. `FRONTEND=legacy` on gateways switches back to the original UI instantly if the new app misbehaves.

## Backup and restore

See [OPERATIONS.md](OPERATIONS.md#backup-and-restore) and [scripts/pg_backup.sh](../scripts/pg_backup.sh),
[scripts/pg_restore_check.sh](../scripts/pg_restore_check.sh). A restore drill must be done before launch; none has been run
against a managed provider yet.

## Routine jobs

| Job | Command | Suggested schedule |
|---|---|---|
| Trim old market history | `python -m src.db.maintenance purge-history --keep-days 90` | daily, after market close |
| Remove expired tokens and idempotency keys | `python -m src.db.maintenance purge-expired` | daily |
| Database backup | `scripts/pg_backup.sh` | at least daily (or the provider's automated backups) |
| Look at sizes | `python -m src.db.maintenance stats` | when investigating growth |
