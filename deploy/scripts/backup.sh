#!/usr/bin/env bash
# Encrypted logical backup of the FlexyVotes database (+ private evidence files).
#
#   DATABASE_URL=postgres://... BACKUP_PASSPHRASE=... [BACKUP_S3_URI=s3://bucket/prefix] \
#   [PRIVATE_MEDIA_DIR=/var/lib/docker/volumes/..._private_media/_data] ./deploy/scripts/backup.sh
#
# Produces <name>.dump.enc (AES-256, PBKDF2) and a .sha256 checksum; optionally
# uploads both to S3 (enable bucket versioning + cross-region replication).
# RDS automated backups/PITR remain the primary mechanism - see
# docs/DISASTER_RECOVERY.md; this script adds portable, independently
# restorable copies.
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE is required}"
OUT_DIR="${BACKUP_DIR:-./backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="flexyvotes-${STAMP}"
mkdir -p "$OUT_DIR"

echo "Dumping database..."
pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" \
  | openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt -pass env:BACKUP_PASSPHRASE \
  > "$OUT_DIR/$NAME.dump.enc"

if [[ -n "${PRIVATE_MEDIA_DIR:-}" && -d "$PRIVATE_MEDIA_DIR" ]]; then
  echo "Archiving private media (evidence)..."
  tar -C "$PRIVATE_MEDIA_DIR" -czf - . \
    | openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt -pass env:BACKUP_PASSPHRASE \
    > "$OUT_DIR/$NAME.media.tgz.enc"
fi

( cd "$OUT_DIR" && sha256sum "$NAME".* > "$NAME.sha256" )
echo "Backup written: $OUT_DIR/$NAME.*"

if [[ -n "${BACKUP_S3_URI:-}" ]]; then
  echo "Uploading to $BACKUP_S3_URI ..."
  aws s3 cp "$OUT_DIR/" "$BACKUP_S3_URI/$STAMP/" --recursive --exclude "*" --include "$NAME.*" --sse aws:kms
fi

# Retention for local copies.
find "$OUT_DIR" -name 'flexyvotes-*' -mtime +"${BACKUP_RETENTION_DAYS:-14}" -delete
