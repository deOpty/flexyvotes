# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

# Prevent Python from writing .pyc files / buffering stdout (keeps container logs live)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System dependencies required to build psycopg2-binary/Pillow wheels and for
# healthchecks (curl). libpq-dev/gcc are only needed if a wheel isn't
# available for the target platform; kept minimal otherwise.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first so this layer is cached across code changes
COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code
COPY . .

# Create a non-root user to run the app
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/staticfiles /app/media \
    && chown -R appuser:appuser /app

USER appuser

# Collect static assets at build time so the image is self-contained
# (SECRET_KEY/DB env vars are not required for collectstatic).
#
# --upload-unhashed-files is required here: django-cloudinary-storage
# registers its own `collectstatic` command (it takes precedence over
# Django's built-in one because `cloudinary_storage` is in INSTALLED_APPS),
# and that command's copy_file() is a no-op unless STATICFILES_STORAGE is
# Cloudinary's own StaticCloudinaryStorage OR this flag is passed. This app
# uses Whitenoise for static files (Cloudinary is only used for media), so
# without the flag `collectstatic` silently copies zero files.
RUN python manage.py collectstatic --noinput --upload-unhashed-files

# Default port the app listens on inside the container; override with the
# PORT env var at run time (e.g. via .env) if 8000 is already taken on the host.
ENV PORT=8000
EXPOSE 8000

COPY --chown=appuser:appuser docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/login/" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["sh", "-c", "gunicorn vote_fund.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 3 --timeout 60 --log-file -"]
