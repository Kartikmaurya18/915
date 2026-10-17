#!/usr/bin/env bash
# Logical backup of the Market Data database (PostgreSQL), as a compressed custom-format dump.
#
#   DATABASE_URL=postgresql://user:pass@host:5432/marketdata  scripts/pg_backup.sh [output-dir]
#
# Writes <output-dir>/marketdata-YYYYmmdd-HHMMSSZ.dump (default ./backups) and prints its path and size.
# Keeps the newest $KEEP dumps (default 14) and deletes older ones in that directory.
#
# This complements, and does not replace, the provider's own automated backups / point-in-time recovery.
# An untested backup is not a backup: run scripts/pg_restore_check.sh against a fresh dump before relying on it.
set -euo pipefail

: "${DATABASE_URL:?set DATABASE_URL to the database to back up}"
OUT_DIR="${1:-backups}"
KEEP="${KEEP:-14}"

# The app accepts postgres:// and the +psycopg driver suffix; libpq tools want a plain postgresql:// URI.
URL="${DATABASE_URL/postgres:\/\//postgresql://}"
URL="${URL/postgresql+psycopg:\/\//postgresql://}"

command -v pg_dump >/dev/null || { echo "pg_dump not found: install the PostgreSQL client tools" >&2; exit 127; }

mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%d-%H%M%SZ)"
FILE="$OUT_DIR/marketdata-$STAMP.dump"
TMP="$FILE.partial"

# Written to a temporary name first so an interrupted run never leaves something that looks like a good backup.
pg_dump --dbname="$URL" --format=custom --compress=6 --no-owner --file="$TMP"
mv "$TMP" "$FILE"

# Reject an empty or implausibly small result.
SIZE=$(wc -c < "$FILE")
if [ "$SIZE" -lt 1024 ]; then
  echo "backup is only $SIZE bytes: something is wrong" >&2
  exit 1
fi
echo "$FILE ($SIZE bytes)"

# Retention: keep the newest $KEEP dumps.
ls -1t "$OUT_DIR"/marketdata-*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f --
