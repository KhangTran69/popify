#!/usr/bin/env bash
# Consistent encrypted archive of stopped n8n state. No external transfer.
set -euo pipefail
umask 077
if [[ $# != 2 ]]; then
  echo "Usage: bash scripts/backup_n8n_encrypted.sh VERIFIED_RECIPIENT_FINGERPRINT OUTPUT.gpg" >&2
  exit 2
fi
recipient=$1
output=$2
[[ "$recipient" =~ ^[A-Fa-f0-9]{40}$ ]] || exit 2
[[ ! -e "$output" && ! -L "$output" ]] || { echo "Refusing to overwrite archive" >&2; exit 2; }
gpg --batch --list-keys "$recipient" >/dev/null 2>&1 || exit 2
id=$(docker compose ps -aq n8n)
[[ -n "$id" && "$(docker inspect --format '{{.State.Running}}' "$id")" == false ]] || {
  echo "Stop n8n cleanly before archiving its SQLite state; do not run during an upgrade." >&2; exit 2;
}
volume=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Name}}{{end}}{{end}}' "$id")
image=$(docker inspect --format '{{.Image}}' "$id")
[[ -n "$volume" && -n "$image" ]] || exit 2
temp=$(mktemp -- "$output.tmp.XXXXXX")
trap 'rm -f -- "$temp"' EXIT
docker run --rm --network none --user 0 --mount "type=volume,source=$volume,target=/state,readonly" \
  --entrypoint sh "$image" -ec 'test -s /state/database.sqlite; tar -C /state -cf - .' |
  gpg --batch --yes --trust-model always --recipient "$recipient" --encrypt --output "$temp"
test -s "$temp"
[[ "$(docker inspect --format '{{.State.Running}}' "$id")" == false ]] || { echo "n8n started during backup; refusing archive" >&2; exit 2; }
python3 "$(dirname "${BASH_SOURCE[0]}")/publish_encrypted_archive.py" "$temp" "$output"
echo "Encrypted n8n state archived; preserve the matching encryption key separately."
