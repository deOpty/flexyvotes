# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus

WORKDIR /app

# libpq5: PostgreSQL client library; gettext: compile translations;
# curl: container health checks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 gettext curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./
# INSTALL_DEV=true adds test/security tooling (used by CI and `make test`).
ARG INSTALL_DEV=false
RUN pip install -r requirements.txt \
    && if [ "$INSTALL_DEV" = "true" ]; then pip install -r requirements-dev.txt; fi

COPY . .

RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/staticfiles /app/media /app/private_media "$PROMETHEUS_MULTIPROC_DIR" \
    && chown -R appuser:appuser /app "$PROMETHEUS_MULTIPROC_DIR"

USER appuser

# The SECRET_KEY here only lets settings load for these build steps; it is not
# baked into the image environment. The real key comes from the runtime env.
RUN SECRET_KEY=build-only-placeholder python manage.py compilemessages --ignore=.venv \
    && SECRET_KEY=build-only-placeholder python manage.py collectstatic --noinput

ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION \
    PORT=8000
EXPOSE 8000

COPY --chown=appuser:appuser docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/healthz/live" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["web"]
