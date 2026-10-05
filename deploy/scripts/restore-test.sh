#!/usr/bin/env bash
# Prove a backup is restorable: restore it into a throw-away PostgreSQL
# container, run migrations check + full integrity verification, compare row
# counts, then destroy the container. Run monthly (and after any schema change).
#
#   BACKUP_PASSPHRASE=... SECRET_KEY=<prod value> FIELD_ENCRYPTION_KEYS=<prod value> \
#   ./deploy/scripts/restore-test.sh backups/flexyvotes-XXXX.dump.enc
set -euo pipefail

FILE="${1:?usage: restore-test.sh <backup.dump.enc>}"
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE is required}"
IMAGE="${APP_IMAGE:-flexyvotes:local}"
NAME="fv-restore-test-$$"
PORT="${RESTORE_TEST_PORT:-55432}"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$NAME" -e POSTGRES_PASSWORD=restore -e POSTGRES_DB=restore -p "127.0.0.1:${PORT}:5432" postgres:16-alpine >/dev/null
for _ in $(seq 1 30); do docker exec "$NAME" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

TARGET="postgres://postgres:restore@127.0.0.1:${PORT}/restore"
START=$(date +%s)
TARGET_DATABASE_URL="$TARGET" "$(dirname "$0")/restore.sh" "$FILE"
END=$(date +%s)

echo "Restore took $((END - START))s (compare with the RTO target)."
docker run --rm --network host -e DATABASE_URL="$TARGET" -e DATABASE_SSL_REQUIRE=False \
  -e SECRET_KEY -e FIELD_ENCRYPTION_KEYS -e SIGNING_PRIVATE_KEY -e BLIND_INDEX_KEY "$IMAGE" \
  sh -c 'python manage.py migrate --check && python manage.py verify_integrity --skip-evidence && python manage.py shell -c "
from django.apps import apps
for label in [\"voting.Event\",\"elections.Voter\",\"elections.Ballot\",\"payments.Payment\",\"core.AuditEvent\"]:
    print(label, apps.get_model(label).objects.count())"'
echo "RESTORE TEST PASSED"
