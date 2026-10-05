#!/usr/bin/env bash
# Restore an encrypted backup into TARGET_DATABASE_URL (an EMPTY database).
#
#   TARGET_DATABASE_URL=postgres://... BACKUP_PASSPHRASE=... ./deploy/scripts/restore.sh backups/flexyvotes-XXXX.dump.enc
#
# Never point this at the live production database.
set -euo pipefail

FILE="${1:?usage: restore.sh <backup.dump.enc>}"
: "${TARGET_DATABASE_URL:?TARGET_DATABASE_URL is required}"
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE is required}"

SUMS="${FILE%.dump.enc}.sha256"
if [[ -f "$SUMS" ]]; then
  echo "Verifying checksum..."
  # Check only the dump's own line (portable to BusyBox, no --ignore-missing).
  ( cd "$(dirname "$FILE")" && grep -F "$(basename "$FILE")" "$(basename "$SUMS")" | sha256sum -c - )
else
  echo "WARNING: no checksum file next to the backup; integrity not verified." >&2
fi

echo "Restoring into target database..."
openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -pass env:BACKUP_PASSPHRASE -in "$FILE" \
  | pg_restore --no-owner --no-privileges --exit-on-error --dbname "$TARGET_DATABASE_URL"
echo "Restore complete. Run: DATABASE_URL=\$TARGET_DATABASE_URL python manage.py verify_integrity"
