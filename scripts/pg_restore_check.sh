#!/usr/bin/env bash
# Restore drill: load a dump into a scratch database and compare it with the source.
#
#   SOURCE_URL=postgresql://user:pass@host:5432/marketdata \
#   SCRATCH_URL=postgresql://user:pass@host:5432/marketdata_restore_check \
#     scripts/pg_restore_check.sh backups/marketdata-20261017-020000Z.dump
#
# SCRATCH_URL must point at an EMPTY database whose name contains "restore" or "test"; the script refuses
# anything else, so it can never overwrite real data. It prints a row count per table for both databases and
# exits non-zero if any differ, or if the schema version differs.
#
# Counts are taken right after the dump was made only if the source is quiet; on a live database small
# differences in fast-growing tables (market_candles, market_chain_snapshots, audit_log) are expected.
# Set STRICT_TABLES to a space-separated list to require exact equality only for those (default: the user data).
set -euo pipefail

DUMP="${1:?usage: pg_restore_check.sh <dump file>}"
: "${SOURCE_URL:?set SOURCE_URL (the database the dump came from)}"
: "${SCRATCH_URL:?set SCRATCH_URL (an empty scratch database)}"
STRICT_TABLES="${STRICT_TABLES:-users paper_positions paper_settings refresh_tokens idempotency_keys}"

norm() { local u="${1/postgres:\/\//postgresql://}"; echo "${u/postgresql+psycopg:\/\//postgresql://}"; }
SOURCE="$(norm "$SOURCE_URL")"
SCRATCH="$(norm "$SCRATCH_URL")"

DB_NAME="${SCRATCH##*/}"; DB_NAME="${DB_NAME%%\?*}"
case "$DB_NAME" in
  *restore*|*test*) ;;
  *) echo "refusing: scratch database name '$DB_NAME' must contain 'restore' or 'test'" >&2; exit 2 ;;
esac
[ "$SOURCE" != "$SCRATCH" ] || { echo "refusing: source and scratch are the same database" >&2; exit 2; }
for tool in pg_restore psql; do command -v "$tool" >/dev/null || { echo "$tool not found" >&2; exit 127; }; done

echo "restoring $DUMP into $DB_NAME ..."
pg_restore --dbname="$SCRATCH" --no-owner --exit-on-error "$DUMP"

count() { psql "$1" -At -c "SELECT count(*) FROM $2"; }
tables=$(psql "$SOURCE" -At -c "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1")

status=0
printf '%-28s %12s %12s\n' table source restored
for t in $tables; do
  a=$(count "$SOURCE" "$t"); b=$(count "$SCRATCH" "$t")
  mark=""
  if [ "$a" != "$b" ]; then
    case " $STRICT_TABLES " in *" $t "*) mark="  <-- MISMATCH"; status=1 ;; *) mark="  (differs; table grows while live)" ;; esac
  fi
  printf '%-28s %12s %12s%s\n' "$t" "$a" "$b" "$mark"
done

va=$(psql "$SOURCE" -At -c "SELECT version_num FROM alembic_version" 2>/dev/null || echo none)
vb=$(psql "$SCRATCH" -At -c "SELECT version_num FROM alembic_version" 2>/dev/null || echo none)
echo "schema version: source=$va restored=$vb"
[ "$va" = "$vb" ] || status=1

if [ "$status" -eq 0 ]; then echo "RESTORE CHECK PASSED"; else echo "RESTORE CHECK FAILED" >&2; fi
exit "$status"
