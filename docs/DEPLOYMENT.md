# Deployment guide

How to run Market Data in production, for any host that runs containers. Nothing here has been provisioned and no
provider has been chosen: that needs decisions only the owner can make (see [Decisions you must make](#decisions-you-must-make)).
Everything below was written against, and where marked verified against, the local Docker Compose stack, **not** a cloud provider.

## Decisions you must make

| Decision | Why it blocks launch |
|---|---|
| **Data licensing.** The app uses the mock feed only. Showing live NSE option prices to other people is redistribution and normally needs a vendor licence. | Legal. Keep `MARKET_DATA_PROVIDER=mock` until settled. The real-provider adapters (`dhan`, `nse`) are deliberately disabled in code. |
| **Hosting provider, region and budget.** | Latency to users and to the data source, cost, and which managed Postgres/Redis you can use. |
| **Managed PostgreSQL and Redis products.** | Redis must be private, password-protected and ideally TLS (`rediss://`): the control channel trusts anyone who can write to it. |
| **Domain, TLS certificates, who operates it.** | Cookies are `Secure` in production, so HTTPS is mandatory. |

The current Vercel deployment serves the legacy single-file UI with request-driven mock ticking. **The new architecture cannot run on
Vercel serverless**: it needs long-lived WebSocket connections, a background ingestor and Redis. Use a container host.

## What runs where

```
                       ┌──────────────────────────── private network ────────────────────────────┐
 users ─HTTPS─► load  │                                                                            │
               balancer ─► gateway 1 ─┐                                                            │
                 (TLS,   ─► gateway 2 ─┼─► Redis (feed bus, lease, control, rate limits)            │
                  WS)    ─► gateway N ─┤                                                            │
                                       └─► PostgreSQL (users, paper trading, market history)        │
                          ingestor A ─┬─► Redis        (A and B contend for the lease; one leads)   │
                          ingestor B ─┘─► PostgreSQL                                                 │
                       └─────────────────────────────────────────────────────────────────────────┘
```

| Component | Command | Count | Notes |
|---|---|---|---|
| Gateway | `uvicorn src.api.server:app --host 0.0.0.0 --port 8000 --ws-max-size 65536 --ws-per-message-deflate false` with `REALTIME_MODE=gateway` (the image's default command) | **3 for 1,000 users**, scale on load | Stateless. Serves the web app, REST and WebSockets. Two containerized gateways carried 1,500 clients at a 1 tick/s feed in testing; see [CAPACITY.md](CAPACITY.md) for what was and was not measured. |
| Ingestor | `python -m src.ingestor` | **2** (one leads, one stands by) | The only process that talks to the data provider; also runs the take-profit/stop-loss exit worker. Serves no users. |
| Redis | any Redis 7 | 1 | No persistence needed (`--save "" --appendonly no`). Private network, password, ideally TLS. |
| PostgreSQL | 16 | 1 managed | The only durable store. Enable provider backups and point-in-time recovery. |
| Migration | `alembic upgrade head` | one-off per release | Never from each replica's entrypoint. |

The image builds the web app and the Python service together (`docker build .`); the same image runs all three roles.

## Release checklist

1. **Build** the image from a commit whose CI is green (`.github/workflows/ci.yml`: backend with Redis and PostgreSQL services, web build and
   tests, live end-to-end, image smoke test).
2. **Migrate** once: `alembic upgrade head` against the production database. Migrations are backward compatible with the previous release.
3. **Ingestors** first, one at a time. The standby takes over within about one lease TTL.
4. **Gateways** next, one at a time behind the load balancer. Sockets reconnect and resubscribe by themselves.
5. **Verify**: `GET /api/ready` is 200 on every gateway; the `NoIngestorLeader` alert is quiet; sign in, place and close a simulated order.
6. **Create the first admin** out of band: `python -m src.auth.cli make-admin you@example.com`.

**Verifying a deployment.** [scripts/verify_stack.py](../scripts/verify_stack.py) exercises the whole topology end to end and exits non-zero on any failure. It checks that both gateways serve
the same epoch, that a paper-book change on gateway 1 reaches the user's socket on gateway 2 (Redis pub/sub), that the ingestor's automatic take-profit/stop-loss exit reaches the user,
that killing the leading ingestor fails over with a new epoch while the in-progress candle continues, and that history answers through the new leader. It was written for the local
Compose stack (`docker compose -p <project> up -d --build`, then `python scripts/verify_stack.py --project <project>`); to point it at a real deployment, give it the two gateway URLs and
adapt the two `docker compose` calls that stop and start the leader.

Configuration is in [production.env.template](../deploy/production.env.template); every variable is explained in
[OPERATIONS.md](OPERATIONS.md#configuration).

## Load balancer

[deploy/nginx/market-data.conf](../deploy/nginx/market-data.conf) is an example. Requirements, whatever you use:

* HTTPS only; redirect HTTP.
* **WebSocket upgrade** on `/ws` and `/ws/v1`, with an idle timeout above 15 s (the heartbeat interval); 120 s is comfortable.
* No sticky sessions needed.
* Pass the client address in `X-Forwarded-For` and set `TRUSTED_PROXY_HOPS` to the number of proxies **you control**. Wrong in either
  direction, rate limits misbehave (everyone shares one address, or the limit can be dodged by a forged header).
* Do not expose `/metrics` publicly; scrape it on the private network with `METRICS_TOKEN`.
* Health checks: `GET /api/health` for liveness, `GET /api/ready` for readiness. Readiness does not fail on a stale feed on purpose: pulling every
  gateway because the data stopped would turn a data outage into a full outage.

## Monitoring

* Scrape every gateway (`/metrics`, bearer token) and every ingestor (`:9100/metrics`).
* Load [deploy/prometheus/alerts.yml](../deploy/prometheus/alerts.yml). Each alert links to a section of [RUNBOOK.md](RUNBOOK.md). A test keeps
  the alert expressions in step with the metric names in the code. The rules have not been run against a live Prometheus yet.
* Useful panels: `rt_ws_clients`, `rt_feed_age_seconds`, `rt_fanout_seconds` p95, `rt_is_leader`, `rt_snapshots_sent_total` by reason, `rt_publish_total` by result.
* Logs are structured JSON on stdout; ship them with your platform's collector.

## Data and backups

* Market history and users live in PostgreSQL. `python -m src.db.maintenance purge-history --keep-days 90` trims history; schedule it daily.
* **Backups:** use the provider's automated backups and point-in-time recovery, and also run [scripts/pg_backup.sh](../scripts/pg_backup.sh) on a schedule. Then do
  a **restore drill** with [scripts/pg_restore_check.sh](../scripts/pg_restore_check.sh) before launch. The scripts' guards and syntax are tested; an actual dump and
  restore of a large database is not.
* Bring existing data across once: `python -m src.db.import_legacy history --sqlite data/market_data.db` and
  `python -m src.db.import_legacy paper-trades --sqlite data/market_data.db --email you@example.com`.

## Security checklist before launch

- [ ] `APP_ENV=production` (start-up refuses unsafe settings), a unique 48+ character `JWT_SECRET` per environment, from a secret manager.
- [ ] Redis reachable only from gateways and ingestors; password set; TLS if it crosses a network you do not control.
- [ ] Database user has only the rights the app needs; `sslmode=require`; backups enabled and a restore drill done.
- [ ] `TRUSTED_PROXY_HOPS` set to the real number of proxies; `/metrics` not public; `METRICS_TOKEN` set.
- [ ] HTTPS everywhere; HSTS only after confirming all subdomains are HTTPS.
- [ ] `REGISTRATION_OPEN` decided on purpose (open registration with the mock feed is low risk; with licensed data it is not).
- [ ] Terms of use and a privacy notice for the emails and trading activity you store; the app is **simulated trading only** and says so on every page.
- [ ] `npm audit` and `pip-audit` clean at release time (both were clean on 2026-10-17).

## Verified, and not

| Verified locally | Not verified |
|---|---|
| Whole test suites against PostgreSQL 16 and Redis 7 in Docker (Phases 1–2); live end-to-end against a real server; headless-Chrome run of the web app | Any cloud provider, managed Postgres/Redis, TLS termination, a real load balancer |
| Single-gateway capacity ([CAPACITY.md](CAPACITY.md)) | Several gateways behind a load balancer; behaviour under real network conditions |
| Backup script guards and syntax | An actual `pg_dump`/restore at production volume; the provider's snapshot/PITR restore |
| Alert expressions match real metric names | Alerts firing in a live Prometheus; the thresholds' fit to real traffic |
| Failover, fencing, history and push notices in tests | A real data provider; market hours with real ticks |
