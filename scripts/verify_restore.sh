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
if [ "${POPIFY_RESTORE_FROM_STDIN:-}" = 1 ]; then
  cat > "$work/backup.dump"
else
  pg_dump -U "$user" -d "$source_db" -Fc -f "$work/backup.dump"
fi
pg_restore -U "$user" --exit-on-error -d "$target" "$work/backup.dump"
# The CI source is quiescent apart from workflow heartbeat rows. Compare business data.
for entry in orders:id deliveries:id support_cases:id approvals:id action_executions:id execution_reconciliations:id audit_events:id operator_accounts:id schema_migrations:version operator_sessions:token_hash login_buckets:key; do
  table=${entry%:*}
  sort_key=${entry#*:}
  original=$(psql -X -U "$user" -d "$source_db" -Atc "SELECT count(*) FROM $table")
  restored=$(psql -X -U "$user" -d "$target" -Atc "SELECT count(*) FROM $table")
  [ "$original" = "$restored" ] || { echo "Restore count mismatch: $table" >&2; exit 1; }
  # CI data is small and quiescent. Compare deterministic row-content digests, never print them.
  query="SELECT md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY $sort_key), '')) FROM $table t"
  original_digest=$(psql -X -U "$user" -d "$source_db" -v ON_ERROR_STOP=1 -Atc "$query")
  restored_digest=$(psql -X -U "$user" -d "$target" -v ON_ERROR_STOP=1 -Atc "$query")
  [ "$original_digest" = "$restored_digest" ] || { echo "Restore content mismatch: $table" >&2; exit 1; }
done
sequence_query="SELECT schemaname,sequencename,last_value FROM pg_sequences WHERE schemaname = 'public' ORDER BY sequencename"
source_sequences=$(psql -X -U "$user" -d "$source_db" -v ON_ERROR_STOP=1 -Atc "$sequence_query")
restored_sequences=$(psql -X -U "$user" -d "$target" -v ON_ERROR_STOP=1 -Atc "$sequence_query")
[ "$source_sequences" = "$restored_sequences" ] || { echo "Restore sequence mismatch" >&2; exit 1; }
# pg_sequences.last_value alone cannot distinguish an initialized sequence
# from an identical value whose next call has not yet been made.
sequence_state() {
  psql -X -U "$user" -d "$1" -v ON_ERROR_STOP=1 -At <<'SQL'
SELECT format('SELECT %L, last_value, is_called FROM %I.%I;', sequencename, schemaname, sequencename)
FROM pg_sequences WHERE schemaname = 'public' ORDER BY sequencename
\gexec
SQL
}
[ "$(sequence_state "$source_db")" = "$(sequence_state "$target")" ] || { echo "Restore sequence initialization mismatch" >&2; exit 1; }
source_sessions=$(psql -X -U "$user" -d "$source_db" -v ON_ERROR_STOP=1 -Atc "SELECT count(*) FROM operator_sessions")
restored_sessions=$(psql -X -U "$user" -d "$target" -v ON_ERROR_STOP=1 -Atc "SELECT count(*) FROM operator_sessions")
[ "$source_sessions" -gt 0 ] && [ "$source_sessions" = "$restored_sessions" ]
# Explicitly reject a source-as-target invocation before the real invalidation.
if sh /tmp/popify-invalidate-restored-identity.sh --confirm-isolated-restore "$source_db" "$source_db"; then
  echo "Source database invalidation unexpectedly allowed" >&2; exit 1
fi
sh /tmp/popify-invalidate-restored-identity.sh --confirm-isolated-restore "$source_db" "$target"
sh /tmp/popify-invalidate-restored-identity.sh --confirm-isolated-restore "$source_db" "$target"
[ "$(psql -X -U "$user" -d "$target" -Atc "SELECT count(*) FROM operator_sessions")" = 0 ]
[ "$(psql -X -U "$user" -d "$target" -Atc "SELECT count(*) FROM login_buckets")" = 0 ]
[ "$(psql -X -U "$user" -d "$source_db" -Atc "SELECT count(*) FROM operator_sessions")" = "$source_sessions" ]
for entry in operator_accounts:id audit_events:id orders:id deliveries:id support_cases:id approvals:id action_executions:id execution_reconciliations:id schema_migrations:version; do
  table=${entry%:*}
  sort_key=${entry#*:}
  query="SELECT md5(coalesce(string_agg(to_jsonb(t)::text, '' ORDER BY $sort_key), '')) FROM $table t"
  [ "$(psql -X -U "$user" -d "$source_db" -v ON_ERROR_STOP=1 -Atc "$query")" = "$(psql -X -U "$user" -d "$target" -v ON_ERROR_STOP=1 -Atc "$query")" ] || {
    echo "Post-invalidation restore content mismatch: $table" >&2; exit 1;
  }
done
echo "PASS: disposable PostgreSQL restore matched business and audit content; restored sessions revoked without changing source; elapsed_seconds=$(($(date +%s)-started))"
