#!/usr/bin/env bash
# Run only against the isolated CI stack. The private key is ephemeral.
set -euo pipefail
[[ "$POPIFY_CONFIRM_DISPOSABLE" = 1 ]]
umask 077
work=$(mktemp -d)
export GNUPGHOME="$work/gnupg"
mkdir -m 700 "$GNUPGHOME"
cleanup() {
  gpgconf --kill gpg-agent || true
  rm -rf -- "$work"
}
trap cleanup EXIT
gpg --batch --pinentry-mode loopback --passphrase '' --quick-generate-key "PoPify CI Backup <backup@example.test>" rsa2048 encr 1d >/dev/null 2>&1
recipient=$(gpg --batch --with-colons --list-keys | awk -F: '$1=="fpr"{print $10;exit}')
bash scripts/backup_encrypted.sh "$recipient" "$work/backup.gpg"
if bash scripts/backup_encrypted.sh "$recipient" "$work/backup.gpg"; then
  echo "Backup unexpectedly overwrote existing output" >&2
  exit 1
fi
docker compose cp scripts/verify_restore.sh db:/tmp/popify-verify-restore.sh
gpg --batch --decrypt "$work/backup.gpg" |
  docker compose exec -T -e POPIFY_CONFIRM_DISPOSABLE=1 -e POPIFY_RESTORE_FROM_STDIN=1 db sh /tmp/popify-verify-restore.sh
echo "PASS: encrypted archive decrypted and restored; no-clobber protection verified"
