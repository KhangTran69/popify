#!/usr/bin/env bash
# Source only from disposable CI restoration exercises. No real keys exported.
prepare_recovery_keyring() {
  local work=$1 recipient=$2 archive=$3
  [[ "${CI:-}" == true && "${POPIFY_CONFIRM_DISPOSABLE:-}" == 1 ]]
  gpg --batch --pinentry-mode loopback --passphrase '' --export-secret-keys "$recipient" > "$work/synthetic-recovery-key.gpg"
  test -s "$work/synthetic-recovery-key.gpg"
  gpgconf --kill gpg-agent || true
  export GNUPGHOME="$work/recovery-keyring"
  mkdir -m 700 "$GNUPGHOME"
  # Empty keyring must not decrypt the archive.
  if gpg --batch --decrypt "$archive" >/dev/null 2>&1; then
    echo "Unexpected decryption without recovery key" >&2; return 1
  fi
  gpg --batch --import "$work/synthetic-recovery-key.gpg" >/dev/null 2>&1
  rm -f "$work/synthetic-recovery-key.gpg"
  # Reject a truncated ciphertext before any restore command can run.
  local size
  size=$(wc -c < "$archive")
  head -c "$((size / 2))" "$archive" > "$work/truncated.gpg"
  if gpg --batch --decrypt "$work/truncated.gpg" >/dev/null 2>&1; then
    echo "Unexpected successful decryption of truncated archive" >&2; return 1
  fi
  rm -f "$work/truncated.gpg"
  # Complete integrity validation prior to passing plaintext to an isolated target.
  gpg --batch --decrypt "$archive" >/dev/null 2>&1
  echo "PASS: independent synthetic recovery keyring, absent key and truncated archive rejection"
}
