#!/bin/sh
# Run inside the disposable Compose db container only. Never restore over the source.
set -eu
[ "${POPIFY_CONFIRM_DISPOSABLE:-}" = "1" ] || { echo "Disposable database confirmation required" >&2; exit 2; }
source_db="${POSTGRES_DB:?}"
user="${POSTGRES_USER:?}"
work=$(mktemp -d)
chmod 700 "$work"
target="popify_restore_$(basename "$work" | tr -cd 'a-zA-Z0-9')"
created=0
cleanup() {
  if [ "$created" = 1 ]; then dropdb -U "$user" "$target"; fi
  rm -rf "$work"
}
trap cleanup EXIT
started=$(date +%s)
createdb -U "$user" "$target"
created=1
pg_dump -U "$user" -d "$source_db" -Fc -f "$work/backup.dump"
pg_restore -U "$user" --exit-on-error -d "$target" "$work/backup.dump"
# The CI source is quiescent apart from workflow heartbeat rows. Compare business data.
for table in orders deliveries support_cases approvals action_executions execution_reconciliations audit_events operator_accounts; do
  original=$(psql -X -U "$user" -d "$source_db" -Atc "SELECT count(*) FROM $table")
  restored=$(psql -X -U "$user" -d "$target" -Atc "SELECT count(*) FROM $table")
  [ "$original" = "$restored" ] || { echo "Restore count mismatch: $table" >&2; exit 1; }
  # CI data is small and quiescent. Compare deterministic row-content digests, never print them.
  query="SELECT md5(coalesce(string_agg(to_jsonb(t)::text, '', ORDER BY id), '')) FROM $table t"
  original_digest=$(psql -X -U "$user" -d "$source_db" -v ON_ERROR_STOP=1 -Atc "$query")
  restored_digest=$(psql -X -U "$user" -d "$target" -v ON_ERROR_STOP=1 -Atc "$query")
  [ "$original_digest" = "$restored_digest" ] || { echo "Restore content mismatch: $table" >&2; exit 1; }
done
source_sessions=$(psql -X -U "$user" -d "$source_db" -v ON_ERROR_STOP=1 -Atc "SELECT count(*) FROM operator_sessions")
restored_sessions=$(psql -X -U "$user" -d "$target" -v ON_ERROR_STOP=1 -Atc "SELECT count(*) FROM operator_sessions")
[ "$source_sessions" -gt 0 ] && [ "$source_sessions" = "$restored_sessions" ]
psql -X -U "$user" -d "$target" -v ON_ERROR_STOP=1 -c "DELETE FROM operator_sessions; DELETE FROM login_buckets;" >/dev/null
[ "$(psql -X -U "$user" -d "$target" -Atc "SELECT count(*) FROM operator_sessions")" = 0 ]
[ "$(psql -X -U "$user" -d "$source_db" -Atc "SELECT count(*) FROM operator_sessions")" = "$source_sessions" ]
echo "PASS: disposable PostgreSQL restore matched business and audit content; restored sessions revoked without changing source; elapsed_seconds=$(($(date +%s)-started))"
