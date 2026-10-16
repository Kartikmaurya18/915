# Operations guide

Topology, protocol and failure behaviour of the live feed are in [REALTIME.md](REALTIME.md).

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
- Two browser tabs refreshing at the same instant can sign the user out (the loser looks like a replay). The SPA
  must serialise refreshes (single-flight / `BroadcastChannel`).
- WebSocket: the first frame must be `{"type":"auth","token":...}` within 5 s; nothing is sent before it. On `/ws/v1` the
  socket is closed (4401) when its access token expires unless the client sends a fresh `auth` frame for the same
  user first (in-band renewal). The legacy `/ws` (embedded mode only) does not enforce token expiry on open sockets.
- Rate limits (login, registration) are **in-process**: with N gateway replicas the effective limit is N times higher.
  Not yet moved to Redis.
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

## Known limits

* Market history (candles, option-chain snapshots) still uses SQLite on the **ingestor's** disk, which is not
  durable on an ephemeral container disk. In `gateway` mode it is reached through the control channel (so it
  is served only while an ingestor leads). Moving it to PostgreSQL is the remaining persistence work.
* In-progress candle state is held by the leading ingestor; a failover restarts the current candle.
* Paper P&L uses floating point, matching the existing simulation; fine for paper trading, not for real money.
* The old `paper_trades` rows in a local SQLite file are global (no owner) and are not imported; there is no
  per-user mapping for them.
