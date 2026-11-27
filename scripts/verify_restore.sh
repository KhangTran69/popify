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
createdb -U "$user" "$target"
created=1
pg_dump -U "$user" -d "$source_db" -Fc -f "$work/backup.dump"
pg_restore -U "$user" --exit-on-error -d "$target" "$work/backup.dump"
# The CI source is quiescent apart from workflow heartbeat rows. Compare business data.
for table in orders deliveries support_cases approvals action_executions execution_reconciliations; do
  original=$(psql -X -U "$user" -d "$source_db" -Atc "SELECT count(*) FROM $table")
  restored=$(psql -X -U "$user" -d "$target" -Atc "SELECT count(*) FROM $table")
  [ "$original" = "$restored" ] || { echo "Restore count mismatch: $table" >&2; exit 1; }
done
psql -X -U "$user" -d "$target" -v ON_ERROR_STOP=1 -Atc "SELECT count(*) FROM operator_accounts" >/dev/null
echo "PASS: disposable PostgreSQL backup restored with matching business record counts"
