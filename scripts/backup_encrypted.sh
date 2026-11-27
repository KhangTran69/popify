#!/usr/bin/env bash
# Local encrypted backup; no upload or production action is performed.
set -euo pipefail
umask 077
if [[ $# != 2 ]]; then
  echo "Usage: bash scripts/backup_encrypted.sh VERIFIED_RECIPIENT_FINGERPRINT OUTPUT.gpg" >&2
  exit 2
fi
recipient=$1
output=$2
[[ "$recipient" =~ ^[A-Fa-f0-9]{40}$ ]] || { echo "Require a verified full GPG recipient fingerprint" >&2; exit 2; }
[[ ! -e "$output" && ! -L "$output" ]] || { echo "Refusing to overwrite backup" >&2; exit 2; }
# Resolve the exact recipient in the trusted local keyring before contacting the database.
gpg --batch --list-keys "$recipient" >/dev/null 2>&1 || { echo "Recipient key not found" >&2; exit 2; }
temp=$(mktemp -- "$output.tmp.XXXXXX")
trap 'rm -f -- "$temp"' EXIT
# Pipefail prevents publishing an archive if pg_dump or encryption fails.
docker compose exec -T db sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' |
  gpg --batch --yes --trust-model always --recipient "$recipient" --encrypt --output "$temp"
test -s "$temp"
# Atomic no-clobber publication within the destination filesystem.
python3 "$(dirname "${BASH_SOURCE[0]}")/publish_encrypted_archive.py" "$temp" "$output"
echo "Encrypted PostgreSQL archive created; validate restore and approved offsite transfer separately."
