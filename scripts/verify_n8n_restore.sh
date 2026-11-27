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
docker compose stop n8n
id=$(docker compose ps -aq n8n)
source=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Name}}{{end}}{{end}}' "$id")
image=$(docker inspect --format '{{.Image}}' "$id")
[[ -n "$source" && -n "$image" ]]
digest() {
  docker run --rm --network none --user 0 --mount "type=volume,source=$1,target=/state,readonly" \
    --entrypoint sh "$image" -ec 'cd /state; sha256sum database.sqlite commerceops-workflows-sha'
}
original=$(digest "$source")
gpg --batch --pinentry-mode loopback --passphrase '' --quick-generate-key "PoPify CI n8n <n8n@example.test>" rsa2048 encr 1d >/dev/null 2>&1
recipient=$(gpg --batch --with-colons --list-keys | awk -F: '$1=="fpr"{print $10;exit}')
started=$(date +%s)
bash scripts/backup_n8n_encrypted.sh "$recipient" "$work/n8n.gpg"
if bash scripts/backup_n8n_encrypted.sh "$recipient" "$work/n8n.gpg"; then
  echo "Archive overwrite unexpectedly allowed" >&2; exit 1
fi
volume="popify-ci-n8n-$(basename "$work" | tr -cd 'a-zA-Z0-9')"
docker volume create "$volume" >/dev/null
gpg --batch --decrypt "$work/n8n.gpg" |
  docker run --rm -i --network none --user 0 --mount "type=volume,source=$volume,target=/state" \
    --entrypoint sh "$image" -ec 'tar -C /state -xf -; chown -R node:node /state'
[[ "$(digest "$volume")" == "$original" ]]
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
[[ "$(digest "$source")" == "$original" ]]
echo "PASS: encrypted n8n state recovered onto a separate volume; actual scheduled processing resumed; source unchanged; elapsed_seconds=$(($(date +%s)-started))"
