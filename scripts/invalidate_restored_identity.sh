#!/bin/sh
# Trusted isolated restore maintenance only; keep API and workers stopped.
set -eu
if [ "$#" != 3 ] || [ "$1" != --confirm-isolated-restore ]; then
  echo "Usage: invalidate_restored_identity.sh --confirm-isolated-restore SOURCE_DB RESTORED_DB" >&2
  exit 2
fi
source_db=$2
target=$3
if [ -z "$source_db" ] || [ "$source_db" = "$target" ]; then
  echo "Source and restore database must be distinct" >&2; exit 2
fi
case "$target" in
  popify_restore_?*) ;;
  *) echo "Require a separately named popify_restore_ database" >&2; exit 2 ;;
esac
case "$target" in
  *[!a-zA-Z0-9_]*) echo "Invalid restore database name" >&2; exit 2 ;;
esac
user="${POSTGRES_USER:?}"
actual=$(psql -X -U "$user" -d "$target" -v ON_ERROR_STOP=1 -Atc 'SELECT current_database()')
[ "$actual" = "$target" ] || { echo "Restore target mismatch" >&2; exit 2; }
psql -X -U "$user" -d "$target" -v ON_ERROR_STOP=1 --single-transaction \
  -c 'SELECT pg_advisory_xact_lock(607291041); DELETE FROM operator_sessions; DELETE FROM login_buckets;' >/dev/null
echo "Restored authentication artifacts invalidated."
