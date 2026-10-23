#!/usr/bin/env bash
# One-command deployment of the whole FlexyVotes stack on a server:
#
#   ./deploy.sh              git pull, build, start everything, wait until healthy
#   ./deploy.sh --no-pull    same, without updating the code
#
# Starts: PostgreSQL, Redis, restore-on-setup, web, Celery worker + beat,
# the backup worker (nightly encrypted backups pushed to GitHub) and pgAdmin.
# On a new server with an empty database, the newest backup is restored
# automatically before the app starts (see docs/DISASTER_RECOVERY.md).
set -euo pipefail
cd "$(dirname "$0")"

PULL=true
for arg in "$@"; do
    case "$arg" in
        --no-pull) PULL=false ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
# Value of KEY in .env ("" when absent - must not trip `set -e`/pipefail).
env_value() { { grep -E "^$1=" .env 2>/dev/null || true; } | tail -n 1 | cut -d= -f2- | tr -d '\r' | sed -E "s/^['\"]|['\"]$//g"; }

command -v docker >/dev/null || { echo "Docker is not installed." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "The Docker Compose plugin is not installed." >&2; exit 1; }

# --- First run: create .env with fresh secrets --------------------------------
if [ ! -f .env ]; then
    say "No .env found - creating one from .env.example"
    cp .env.example .env
    chmod 600 .env
    secret() { openssl rand -base64 48 | tr -d '\n/+=' | cut -c1-"$1"; }
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(secret 64)|" .env
    sed -i "s|^BACKUP_PASSPHRASE=.*|BACKUP_PASSPHRASE=$(secret 40)|" .env
    sed -i "s|^PGADMIN_DEFAULT_PASSWORD=.*|PGADMIN_DEFAULT_PASSWORD=$(secret 24)|" .env
    cat <<'EOF'

.env was created with a new SECRET_KEY, BACKUP_PASSPHRASE and pgAdmin password.
  * Restoring on a NEW server? Replace SECRET_KEY, BACKUP_PASSPHRASE and the
    encryption keys with the values from the old server - backups and the
    encrypted data inside them are useless without them.
  * Set ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, SITE_URL, PORT and BACKUP_GIT_REPO /
    BACKUP_GIT_TOKEN, then run ./deploy.sh again.
  * Store a copy of .env in your password manager.
EOF
    exit 1
fi

# --- Pre-flight checks ---------------------------------------------------------
secret_key="$(env_value SECRET_KEY)"
if [ -z "$secret_key" ] || [[ "$secret_key" == change-me* ]]; then
    echo "SECRET_KEY in .env is missing or still the placeholder." >&2
    exit 1
fi
passphrase="$(env_value BACKUP_PASSPHRASE)"
if [ -z "$passphrase" ]; then
    warn "BACKUP_PASSPHRASE is not set: nightly backups are DISABLED until you set it in .env."
elif [ "${#passphrase}" -lt 16 ]; then
    warn "BACKUP_PASSPHRASE is shorter than 16 characters: backups will be refused."
fi
[ -n "$(env_value BACKUP_GIT_REPO)" ] || warn "BACKUP_GIT_REPO is not set: backups stay on this server only (no GitHub copy)."

# --- Update code -------------------------------------------------------------------
if $PULL && [ -d .git ]; then
    say "Updating code"
    git pull --ff-only
fi

# --- Build and start everything ---------------------------------------------------
say "Building images"
# web/worker/beat share one image and backup/restore another: build each once
# (building all five in parallel can exhaust memory on small servers).
docker compose build web backup

say "Starting all services"
if ! docker compose up -d --remove-orphans; then
    docker compose logs --tail 60 restore
    echo "Start-up failed - see the output above (a failed restore-on-setup blocks the app on purpose)." >&2
    exit 1
fi

say "Waiting for the web service to become healthy"
web="$(docker compose ps -q web)"
for _ in $(seq 1 60); do
    state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$web" 2>/dev/null || echo missing)"
    [ "$state" = healthy ] && break
    if [ "$state" = unhealthy ] || [ "$state" = exited ] || [ "$state" = missing ]; then
        docker compose logs --tail 80 restore web
        echo "web is $state - see the logs above." >&2
        exit 1
    fi
    sleep 5
done
[ "$state" = healthy ] || { docker compose logs --tail 80 web; echo "web did not become healthy in time." >&2; exit 1; }

say "Restore-on-setup"
docker compose logs --no-log-prefix restore | tail -n 5

say "Integrity check"
docker compose exec -T web python manage.py verify_integrity --skip-evidence | tail -n 4 \
    || warn "verify_integrity reported problems (see above)."

say "Status"
docker compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
port="$(env_value PORT)"; pg_port="$(env_value PGADMIN_PORT)"; schedule="$(env_value BACKUP_SCHEDULE)"
cat <<EOF

Deployed.
  App:      http://<server>:${port:-8000}/   (console: /console/, health: /console/health/)
  pgAdmin:  http://<server>:${pg_port:-5051}/  (server "FlexyVotes" is pre-registered; password: flexyvotes)
  Backups:  cron "${schedule:-0 2 * * *}" - run one now with:
            docker compose exec backup fv-backup nightly
EOF
