# Operations guide (Phase 1)

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
- WebSocket: first frame must be `{"type":"auth","token":...}` within 5 s; nothing is sent before it. An open
  socket is **not** closed when its access token expires (Phase 2: in-band token renewal).
- Rate limits (login, registration) are **in-process**: with N replicas the effective limit is N times higher.
  Moves to Redis in Phase 2.
- Request bodies over 64 KiB are rejected by `Content-Length`; a reverse proxy should enforce the same for
  chunked uploads.

## Known Phase 1 limits

- Per-user books are cached **in each process**, and TP/SL exits are written from the tick thread. Run a
  **single app instance** until Phase 2 moves the engine into a dedicated ingestor and makes the gateway stateless.
- Market history (candles, option-chain snapshots) still uses the local SQLite file, which is **not durable** on an
  ephemeral container disk. User data (accounts, positions, trades, settings, audit) is in PostgreSQL.
- Paper P&L uses floating point, matching the existing simulation; fine for paper trading, not for real money.
- The old `paper_trades` rows in a local SQLite file are global (no owner) and are not imported; there is no
  per-user mapping for them.
