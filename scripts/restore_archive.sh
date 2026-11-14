#!/usr/bin/env bash
# Restore the old pipeline's pg_dump into a SEPARATE database, detroitpulse_archive,
# for analysis and evals only. The live database (detroitpulse) always starts fresh
# from migrations: the old rows carry Google-derived coordinates, which Google's
# terms don't allow keeping beyond 30 days, so they must never flow into live data.
#
#   scripts/restore_archive.sh /path/to/detroitpulse.dump
#
# Reads POSTGRES_* from .env. Safe to re-run: it refuses if the archive DB exists
# unless --replace is given.
set -euo pipefail
cd "$(dirname "$0")/.."

DUMP="${1:?usage: restore_archive.sh <dump file> [--replace]}"
REPLACE="${2:-}"
set -a; . ./.env; set +a
export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD missing from .env}"
export PGHOST="${POSTGRES_HOST:-127.0.0.1}" PGPORT="${POSTGRES_PORT:-5436}" PGUSER="${POSTGRES_USER:-pulse}"
DB=detroitpulse_archive

if psql -d postgres -Atc "select 1 from pg_database where datname='$DB'" | grep -q 1; then
  if [[ "$REPLACE" != "--replace" ]]; then
    echo "$DB already exists; pass --replace to drop and restore again" >&2
    exit 1
  fi
  dropdb "$DB"
fi
createdb -T template0 "$DB"
# pg_restore 16.13 emits `SET transaction_timeout` (a PG17 setting); the server
# rejects that line and the restore continues, so those errors are expected.
nice -n 19 pg_restore --no-owner --no-acl -j 4 -d "$DB" "$DUMP" || true
uv run alembic -x db="$DB" upgrade head
psql -d "$DB" -Atc "select 'transcript_chunks', count(*) from transcript_chunks
                    union all select 'incidents', count(*) from incidents"
