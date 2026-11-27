#!/usr/bin/env bash
# CI-only recovery onto a new volume, preserving the original stopped volume.
set -euo pipefail
[[ "${POPIFY_CONFIRM_DISPOSABLE:-}" == 1 && "${CI:-}" == true ]]
umask 077
work=$(mktemp -d)
export GNUPGHOME="$work/gnupg"
mkdir -m 700 "$GNUPGHOME"
volume=""
switched=0
cleanup() {
  if [[ "$switched" == 1 ]]; then
    docker compose -f docker-compose.yml -f "$work/restore.yml" stop n8n || true
    docker compose -f docker-compose.yml -f "$work/restore.yml" rm -f n8n || true
  fi
  if [[ -n "$volume" ]]; then docker volume rm "$volume" >/dev/null || true; fi
  gpgconf --kill gpg-agent || true
  rm -rf -- "$work"
}
trap cleanup EXIT
id=$(docker compose ps -aq n8n)
source=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Name}}{{end}}{{end}}' "$id")
image=$(docker inspect --format '{{.Image}}' "$id")
[[ -n "$source" && -n "$image" ]]
digest() {
  # Compare every regular file in the stopped synthetic volume, not only the
  # database and import marker. Hash filenames and contents without printing them.
  docker run --rm --network none --user 0 --mount "type=volume,source=$1,target=/state,readonly" \
    --entrypoint sh "$image" -ec 'cd /state; test -s database.sqlite; test -s commerceops-workflows-sha; find . -type f -exec sha256sum {} \; | LC_ALL=C sort | sha256sum'
}
api_image=$(docker inspect --format '{{.Image}}' "$(docker compose ps -q api)")
sqlite_integrity() {
  docker run --rm --network none --read-only --user 0 --mount "type=volume,source=$1,target=/state,readonly" \
    --entrypoint python "$api_image" -c 'import sqlite3; db = sqlite3.connect("file:/state/database.sqlite?mode=ro&immutable=1", uri=True); assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]; db.close()'
}
gpg --batch --pinentry-mode loopback --passphrase '' --quick-generate-key "PoPify CI n8n <n8n@example.test>" rsa2048 encr 1d >/dev/null 2>&1
recipient=$(gpg --batch --with-colons --list-keys | awk -F: '$1=="fpr"{print $10;exit}')
started=$(date +%s)
source scripts/verify_recovery_keyring.sh
prepare_backup_keyring "$work" "$recipient"
if bash scripts/backup_n8n_encrypted.sh "$recipient" "$work/running.gpg"; then
  echo "Backup of running n8n unexpectedly permitted" >&2; exit 1
fi
[[ ! -e "$work/running.gpg" ]]
docker compose stop n8n
# immutable SQLite reads are valid only on the stopped, checkpointed state.
# Refuse a nonempty WAL rather than overlooking uncheckpointed records.
docker run --rm --network none --user 0 --mount "type=volume,source=$source,target=/state,readonly" \
  --entrypoint sh "$image" -ec 'test ! -s /state/database.sqlite-wal'
sqlite_integrity "$source"
original=$(digest "$source")
bash scripts/backup_n8n_encrypted.sh "$recipient" "$work/n8n.gpg"
if bash scripts/backup_n8n_encrypted.sh "$recipient" "$work/n8n.gpg"; then
  echo "Archive overwrite unexpectedly allowed" >&2; exit 1
fi
prepare_recovery_keyring "$work" "$recipient" "$work/n8n.gpg"
volume="popify-ci-n8n-$(basename "$work" | tr -cd 'a-zA-Z0-9')"
docker volume create "$volume" >/dev/null
gpg --batch --decrypt "$work/n8n.gpg" |
  docker run --rm -i --network none --user 0 --mount "type=volume,source=$volume,target=/state" \
    --entrypoint sh "$image" -ec 'tar -C /state -xf -; chown -R node:node /state'
[[ "$(digest "$volume")" == "$original" ]]
sqlite_integrity "$volume"
echo "PASS: running-volume backup rejected; complete stopped-volume file digest and SQLite integrity match before startup"
cat > "$work/restore.yml" <<EOF
volumes:
  n8n_data:
    external: true
    name: $volume
EOF
docker compose exec -T api python -m scripts.verify_outage queue --key ci-n8n-restored-volume --confirm-disposable
switched=1
docker compose -f docker-compose.yml -f "$work/restore.yml" up -d --no-deps --force-recreate --wait --wait-timeout 180 n8n
restored_id=$(docker compose ps -q n8n)
actual=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Name}}{{end}}{{end}}' "$restored_id")
[[ "$actual" == "$volume" ]]
docker compose exec -T api python -m scripts.verify_outage recovered --key ci-n8n-restored-volume --confirm-disposable
docker compose exec -T api python -m scripts.verify_restored_workflows --confirm-disposable
[[ "$(digest "$source")" == "$original" ]]
echo "PASS: encrypted n8n state recovered onto a separate volume; actual scheduled processing resumed; source unchanged; elapsed_seconds=$(($(date +%s)-started))"
