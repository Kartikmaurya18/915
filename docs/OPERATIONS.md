# Operations guide

Topology, protocol and failure behaviour of the live feed are in [REALTIME.md](REALTIME.md). Going to production is in
[DEPLOYMENT.md](DEPLOYMENT.md), incident handling in [RUNBOOK.md](RUNBOOK.md), measured capacity in [CAPACITY.md](CAPACITY.md).

What is verified and what is not is stated explicitly. "Verified" means it was run
against a local PostgreSQL 16 container, **not** against a cloud provider.

## Configuration

| Variable | Purpose |
|---|---|
| `APP_ENV` | `production` forces `AUTH_MODE=required` and secure cookies; the app refuses to start otherwise. |
| `AUTH_MODE` | `required` (real accounts, per-user paper trading) or `disabled` (local dev / legacy UI only). |
| `JWT_SECRET` | >= 32 random characters. `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Rotating it signs everyone out of access tokens (15 min). |
| `DATABASE_URL` | SQLAlchemy URL. `postgres://` and `postgresql://` are accepted. Use `?sslmode=require` for managed providers. |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_STATEMENT_TIMEOUT_MS` | Pool and per-statement timeout (PostgreSQL). |
| `CORS_ORIGINS` | Comma-separated allow-list. Empty = same-origin only. |
| `REALTIME_MODE` | `embedded` (default: one process owns the engine and serves clients) or `gateway` (serves clients only; data comes from Redis; requires `AUTH_MODE=required`). The ingestor is the separate `python -m src.ingestor` entrypoint. |
| `REDIS_URL`, `RT_PREFIX` | Redis for the live feed, the ingestor lease and the control channel. `RT_PREFIX` namespaces the keys (default `rt:`). Redis must be private and password-protected: the control channel trusts whoever can write to it. |
| `INGESTOR_ID`, `INGESTOR_LEASE_TTL_MS`, `INGESTOR_METRICS_PORT` | Ingestor identity, lease length (failover takes about one TTL after a crash) and its metrics port. |
| `METRICS_TOKEN` | Bearer token for `/metrics`. Without it `/metrics` does not exist when auth is on. |
| `WS_MAX_CONNECTIONS_PER_USER`, `WS_MAX_CONNECTIONS_TOTAL` | Per-account and per-process WebSocket caps. |
| `RATE_LIMIT_BACKEND` | `auto` (default: Redis when `REDIS_URL` is set), `redis` or `memory`. With Redis the login/registration limits are shared by every gateway; if Redis is unreachable the limiter falls back to per-process counters and recovers by itself (metric `rate_limit_backend_errors_total`). |
| `MARKET_HISTORY_BACKEND` | `sql` stores candles and ATM snapshots in the shared database (default when `AUTH_MODE=required` and `DATABASE_URL` is set); `sqlite` keeps the original local file (default for local development). |
| `RT_BATCH_WINDOW_MS` | How long a batching client waits so one tick's topics go out in one message (default 4). `0` disables the wait. |
| `LIVE_DATA_ENABLED` | `true` allows the real Dhan market-data provider (off by default; mock and replay never need it). Showing real prices to other people needs a data licence this code cannot check; read [LIVE_DATA.md](LIVE_DATA.md) first. Needs `pip install -r requirements-live.txt`. |
| `GREEKS_SOURCE` | `calculated` (default: provider values plus a Black-Scholes calculator for gaps) or `provider` (only what the provider sends; mock then has no Greeks). |
| `RISK_FREE_RATE`, `DIVIDEND_YIELD` | Inputs to the calculator (defaults 0.065 and 0.012). The dividend yield is only a fallback; the engine derives the market's carry from put-call parity when the quotes allow. |
| `VERIFICATION_MODE` | `both` (default) / `email` / `phone` / `off`: which contact details a new account must prove with one-time codes. In production the app refuses to start unless every required channel has a real provider. Full guide: [AUTH.md](AUTH.md). |
| `EMAIL_PROVIDER`, `SMS_PROVIDER` + their settings (`SMTP_*`, `EMAIL_FROM`, `TWILIO_*`, `SMS_WEBHOOK_*`) | How codes are delivered. `console` prints them to the log and is allowed only outside production. |
| `OTP_TTL_MINUTES`, `OTP_MAX_ATTEMPTS`, `OTP_RESEND_SECONDS`, `OTP_MAX_SENDS_PER_HOUR` | Code lifetime (10), guesses per code (5), resend cooldown (60 s), codes per hour per account and channel (5). |
| `FRONTEND` | `spa` (default when `web/dist` exists) serves the React app; `legacy` serves the original single-file UI from `public/`. |
| `TRUSTED_PROXY_HOPS` | Reverse proxies in front of the app. `0` ignores `X-Forwarded-For` (rate limits then see the proxy's IP, so set this correctly behind a load balancer). |

Secrets come from the environment or a secret manager. `.env` is git-ignored and must never be committed.
Use a separate database (and credentials) per environment; never point tests or local development at production.

## Migrations

Schema changes are Alembic migrations in `migrations/versions/`.

```
alembic upgrade head        # apply
alembic downgrade -1        # roll back one step
alembic check               # fail if the models differ from the migrations
```

- The app **checks the schema at startup** (`AUTH_MODE=required`) and refuses to start if it is not at head;
  `/api/ready` returns 503 with `schema_outdated` in the same situation.
- **Run migrations as a separate release step, once**, before starting new app instances. Do not run them from
  every replica's entrypoint: concurrent `upgrade`s race. (`docker-compose.yml` does this with a one-shot
  `migrate` service.)
- Keep migrations backward compatible with the previous release (add columns/tables first, remove in a later
  release) so a rolling deploy or a rollback of the app never meets a schema it cannot read.
- A migration that drops or rewrites data needs its own reviewed plan; do not auto-apply it.

## First admin

Admin rights are granted only out-of-band, never via the API or an email allow-list:

```
python -m src.auth.cli make-admin someone@example.com
python -m src.auth.cli remove-admin someone@example.com
```

Admin-only: `POST /api/settings/*` (global data source, ATM method, candle basis) and `GET /api/logs`.
The role is re-read from the database on each admin request, so demotion is immediate.

## Backup and restore

Managed providers take automated backups; confirm the retention and point-in-time-recovery window of your plan
and **do a restore drill before launch** - an untested backup is not a backup.

Logical backup / restore, verified locally (PostgreSQL 16):

```
pg_dump  -U <user> -d <db> -Fc -f marketdata.dump
createdb -U <user> marketdata_restore
pg_restore -U <user> -d marketdata_restore --no-owner marketdata.dump
```

Verification performed: row counts for every table, the `alembic_version`, and an aggregate of P&L were identical
between source and restore, and an app instance pointed at the restored database served the user's history after
a normal login.

**Not verified:** a restore from a cloud provider's own snapshot/PITR, and restore time at production data volume.

## Security properties (and their limits)

- Passwords: argon2id. Refresh tokens: opaque, stored only as SHA-256, rotated on every use; replaying a spent
  token revokes the whole session family. The refresh token lives in an HttpOnly, SameSite=Lax cookie scoped to
  `/api/auth`; refresh/logout also require an `X-Requested-With` header (forces a CORS preflight, blocking CSRF).
- Access tokens: 15 minutes, in memory on the client. A deactivated user's *existing* access token stays valid
  until it expires; refresh is refused immediately.
- Two browser tabs refreshing at the same instant would look like a replayed token and sign the user out. The web app
  serialises refreshes across tabs with the Web Locks API and shares one refresh among simultaneous requests in a tab.
- WebSocket: the first frame must be `{"type":"auth","token":...}` within 5 s; nothing is sent before it. On `/ws/v1` the
  socket is closed (4401) when its access token expires unless the client sends a fresh `auth` frame for the same
  user first (in-band renewal). The legacy `/ws` (embedded mode only) does not enforce token expiry on open sockets.
- Rate limits (login per account, login per IP, registration per IP) are shared through Redis when it is configured
  (atomic Lua on a sorted set, using Redis's own clock), so they hold across gateway replicas. Account identifiers are hashed before
  they reach Redis. If Redis is unreachable the limits degrade to per-process counters (N times higher with N replicas) until it returns.
- Request bodies over 64 KiB are rejected by `Content-Length`; a reverse proxy should enforce the same for
  chunked uploads.

## Database behaviour under concurrency

* Mutating auth operations run through `run_in_transaction`, which retries the whole transaction when
  PostgreSQL picks it as a deadlock victim or reports a serialization failure (`db_transaction_retries_total`).
  Revoking a session family takes its row locks in a fixed order first, which is what makes concurrent
  revocations (every loser of a refresh race runs one) deadlock-free in the common case.
* Closing a paper position is a conditional `UPDATE ... WHERE status = 'OPEN'`; a manual close and the
  exit worker racing each other have exactly one winner.
* Statements are cancelled after `DB_STATEMENT_TIMEOUT_MS` (default 10 s). A request that waits that long
  for a lock fails with an error instead of holding a pooled connection indefinitely.

## Market history, imports and housekeeping

Candles and ATM snapshots are written by the leading ingestor. With `MARKET_HISTORY_BACKEND=sql` they live in the shared database, so a failover, a
redeploy or a second ingestor all see the same history; the migration is `0002`. History writes never block the live feed: a failed write is logged and
skipped, which leaves a gap in stored history (not in what live users see).

```
python -m src.db.import_legacy history       --sqlite data/market_data.db          # copy the old local history (safe to repeat)
python -m src.db.import_legacy paper-trades  --sqlite data/market_data.db --email you@example.com
python -m src.db.maintenance purge-history --keep-days 90      # delete whole past days outside the window; never today
python -m src.db.maintenance purge-expired                     # old idempotency keys and refresh tokens
python -m src.db.maintenance stats
```

A new leader restores today's completed candles from history and continues the previous leader's in-progress candle from what it last published, so a
failover does not blank the chart.

## Known limits

* Per-strike straddle candles for the *live* chart are rebuilt in memory by the leader; after a failover the live straddle series restarts (completed
  candles are in history and show in the History page).
* The in-progress candle is continued from the last published state; ticks the previous leader saw after its last publish are lost.
* Paper P&L uses floating point, matching the existing simulation; fine for paper trading, not for real money.
* Real data providers are deliberately disabled; everything has run on the mock feed.
* Nothing in this repository has been run on a cloud provider; see [DEPLOYMENT.md](DEPLOYMENT.md#verified-and-not).
