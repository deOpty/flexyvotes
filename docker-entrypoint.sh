#!/bin/bash
set -euo pipefail

# Only run migrations/admin-seeding when actually starting the server, not
# for one-off commands (e.g. `docker compose run web python manage.py test`).
if [[ "$*" == *gunicorn* ]]; then
    echo "Applying database migrations..."
    python manage.py migrate --noinput

    if [[ -n "${DJANGO_SUPERUSER_USERNAME:-}" && -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]]; then
        echo "Ensuring admin superuser exists..."
        python manage.py seed_admin
    fi
fi

echo "Starting: $*"
exec "$@"
