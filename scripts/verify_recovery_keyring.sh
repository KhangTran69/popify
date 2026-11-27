#!/usr/bin/env bash
# Source only from disposable CI restoration exercises. No real keys exported.
prepare_backup_keyring() {
  local work=$1 recipient=$2
  [[ "${CI:-}" == true && "${POPIFY_CONFIRM_DISPOSABLE:-}" == 1 ]]
  gpg --batch --export "$recipient" > "$work/synthetic-public-key.gpg"
  gpg --batch --pinentry-mode loopback --passphrase '' --export-secret-keys "$recipient" > "$work/synthetic-recovery-key.gpg"
  test -s "$work/synthetic-public-key.gpg"
  test -s "$work/synthetic-recovery-key.gpg"
  gpgconf --kill gpg-agent || true
  export GNUPGHOME="$work/backup-keyring"
  mkdir -m 700 "$GNUPGHOME"
  gpg --batch --import "$work/synthetic-public-key.gpg" >/dev/null 2>&1
  if gpg --batch --with-colons --list-secret-keys | grep -q '^sec:'; then
    echo "Backup keyring unexpectedly contains a secret key" >&2; return 1
  fi
}

prepare_recovery_keyring() {
  local work=$1 recipient=$2 archive=$3
  [[ "${CI:-}" == true && "${POPIFY_CONFIRM_DISPOSABLE:-}" == 1 ]]
  if gpg --batch --decrypt "$archive" >/dev/null 2>&1; then
    echo "Public-only backup keyring unexpectedly decrypted archive" >&2; return 1
  fi
  gpgconf --kill gpg-agent || true
  export GNUPGHOME="$work/recovery-keyring"
  mkdir -m 700 "$GNUPGHOME"
  if gpg --batch --decrypt "$archive" >/dev/null 2>&1; then
    echo "Unexpected decryption without recovery key" >&2; return 1
  fi
  gpg --batch --pinentry-mode loopback --passphrase '' --quick-generate-key "Unrelated CI recipient <wrong@example.test>" rsa2048 encr 1d >/dev/null 2>&1
  if gpg --batch --decrypt "$archive" >/dev/null 2>&1; then
    echo "Unexpected decryption with unrelated key" >&2; return 1
  fi
  gpg --batch --import "$work/synthetic-recovery-key.gpg" >/dev/null 2>&1
  rm -f "$work/synthetic-recovery-key.gpg"
  gpg --batch --list-secret-keys "$recipient" >/dev/null 2>&1
  local size
  size=$(wc -c < "$archive")
  head -c "$((size / 2))" "$archive" > "$work/truncated.gpg"
  if gpg --batch --decrypt "$work/truncated.gpg" >/dev/null 2>&1; then
    echo "Unexpected successful decryption of truncated archive" >&2; return 1
  fi
  rm -f "$work/truncated.gpg"
  python3 - "$archive" "$work/tampered.gpg" <<'PY'
from pathlib import Path
import sys
data = bytearray(Path(sys.argv[1]).read_bytes())
assert len(data) > 128
data[len(data) // 2] ^= 1
Path(sys.argv[2]).write_bytes(data)
PY
  if gpg --batch --decrypt "$work/tampered.gpg" >/dev/null 2>&1; then
    echo "Unexpected successful decryption of modified archive" >&2; return 1
  fi
  rm -f "$work/tampered.gpg"
  gpg --batch --decrypt "$archive" >/dev/null 2>&1
  echo "PASS: public-only backup, independent recovery keyring, absent/wrong key and truncated/modified archive rejection"
}
