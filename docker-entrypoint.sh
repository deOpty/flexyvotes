#!/bin/bash
set -euo pipefail

# Roles:  web | worker | beat | migrate | <any other command>
role="${1:-web}"

prepare_metrics_dir() {
    # Prometheus multiprocess mode keeps per-process files; start clean.
    mkdir -p "${PROMETHEUS_MULTIPROC_DIR:-/tmp/prometheus}"
    rm -f "${PROMETHEUS_MULTIPROC_DIR:-/tmp/prometheus}"/*.db 2>/dev/null || true
}

case "$role" in
    web)
        prepare_metrics_dir
        if [[ "${RUN_MIGRATIONS:-true}" == "true" ]]; then
            echo "Applying database migrations..."
            python manage.py migrate --noinput
        fi
        # Creates/syncs every DJANGO_SUPERUSER_* and DJANGO_SUPERUSER_<n>_* admin;
        # does nothing when none are configured.
        echo "Ensuring admin accounts exist..."
        python manage.py seed_admin
        exec gunicorn vote_fund.wsgi:application --config /app/gunicorn.conf.py
        ;;
    worker)
        prepare_metrics_dir
        exec celery -A vote_fund worker --loglevel="${CELERY_LOG_LEVEL:-info}" \
            --queues=default,notifications,payments --concurrency="${CELERY_CONCURRENCY:-4}" \
            --max-tasks-per-child=1000
        ;;
    beat)
        exec celery -A vote_fund beat --loglevel="${CELERY_LOG_LEVEL:-info}" \
            --schedule=/tmp/celerybeat-schedule
        ;;
    migrate)
        exec python manage.py migrate --noinput
        ;;
    *)
        exec "$@"
        ;;
esac
