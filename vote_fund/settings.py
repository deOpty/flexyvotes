"""
FlexyVotes settings.

Everything environment-specific is read from environment variables (a local
``.env`` is loaded for development). See docs/OPERATIONS.md for the full
variable reference.
"""
import os
import sys
from pathlib import Path

import dj_database_url
from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

TESTING = len(sys.argv) > 1 and sys.argv[1] == 'test'


def env(name, default=None):
    value = os.getenv(name)
    return default if value is None or value == '' else value.strip()


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None or value.strip() == '':
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def env_int(name, default):
    value = os.getenv(name)
    try:
        return int(value) if value not in (None, '') else default
    except ValueError:
        raise ImproperlyConfigured(f'{name} must be an integer.')


def env_list(name, default=''):
    return [item.strip() for item in (os.getenv(name) or default).split(',') if item.strip()]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
DEBUG = env_bool('DEBUG', False)

SECRET_KEY = env('SECRET_KEY')
if not SECRET_KEY:
    if DEBUG or TESTING:
        # Only reachable with DEBUG or under tests.
        SECRET_KEY = 'insecure-development-only-secret-key-change-me'  # nosec B105
    else:
        raise ImproperlyConfigured('SECRET_KEY must be set when DEBUG is False.')

ALLOWED_HOSTS = env_list('ALLOWED_HOSTS', 'localhost,127.0.0.1,0.0.0.0')
if TESTING:
    ALLOWED_HOSTS = ['*']

PLATFORM_NAME = env('PLATFORM_NAME', 'FlexyVotes')
# Public base URL used to build absolute links (payment callbacks, emails, SSO).
SITE_URL = env('SITE_URL', 'http://localhost:8000').rstrip('/')
# Moving the Django admin off /admin/ cuts automated scanning noise.
ADMIN_URL = env('ADMIN_URL', 'admin/').strip('/') + '/'

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.humanize',
    'cloudinary',
    # Listed after staticfiles so Django's own collectstatic wins: static files
    # are served by WhiteNoise, and django-cloudinary-storage's override reads
    # STATICFILES_STORAGE, which Django 5.1+ no longer defines. Cloudinary is
    # still used for media via STORAGES['default'].
    'django.contrib.staticfiles',
    'cloudinary_storage',
    'ninja',
    'core',
    'voting',
    'elections',
    'payments',
    'fraud',
    'notifications',
    'billing',
    'api',
]

MIDDLEWARE = [
    'core.middleware.HealthCheckMiddleware',
    'core.middleware.CorrelationIdMiddleware',
    'core.middleware.MetricsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'core.middleware.BackpressureMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'core.middleware.ApiCorsMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'core.middleware.SessionSecurityMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'core.middleware.UserPreferencesMiddleware',
    'core.middleware.SecurityHeadersMiddleware',
]

ROOT_URLCONF = 'vote_fund.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.template.context_processors.i18n',
                'django.template.context_processors.tz',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.platform',
            ],
        },
    },
]

WSGI_APPLICATION = 'vote_fund.wsgi.application'

# ---------------------------------------------------------------------------
# Database (PostgreSQL in every deployed environment; SQLite for quick local dev)
# ---------------------------------------------------------------------------
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}
if env('DATABASE_URL'):
    DATABASES['default'] = dj_database_url.parse(
        env('DATABASE_URL'),
        conn_max_age=env_int('DB_CONN_MAX_AGE', 600),
        conn_health_checks=True,
        ssl_require=env_bool('DATABASE_SSL_REQUIRE', True),
    )
    DATABASES['default'].setdefault('OPTIONS', {})
    # Server-side statement timeout keeps a slow analytic query from pinning
    # a connection the vote path needs.
    if DATABASES['default']['ENGINE'].endswith('postgresql'):
        timeout_ms = env_int('DB_STATEMENT_TIMEOUT_MS', 30000)
        DATABASES['default']['OPTIONS']['options'] = f'-c statement_timeout={timeout_ms}'
        # Required behind PgBouncer in transaction-pooling mode.
        DATABASES['default']['DISABLE_SERVER_SIDE_CURSORS'] = env_bool('DB_DISABLE_SERVER_SIDE_CURSORS', False)
        DATABASES['default']['TEST'] = {'NAME': env('TEST_DATABASE_NAME', 'test_flexyvotes')}

# Optional read replica: results/analytics/report reads are routed here.
if env('DATABASE_REPLICA_URL') and not TESTING:
    DATABASES['replica'] = dj_database_url.parse(
        env('DATABASE_REPLICA_URL'),
        conn_max_age=env_int('DB_CONN_MAX_AGE', 600),
        conn_health_checks=True,
        ssl_require=env_bool('DATABASE_SSL_REQUIRE', True),
    )
DATABASE_ROUTERS = ['core.db_router.ReplicaRouter']

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------------------------------------------------------------------
# Cache / Redis
# ---------------------------------------------------------------------------
REDIS_URL = env('REDIS_URL')
if REDIS_URL and not TESTING:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL,
            'KEY_PREFIX': 'fv',
            'TIMEOUT': 300,
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'flexyvotes-default',
        }
    }

SESSION_ENGINE = 'django.contrib.sessions.backends.cached_db'

# ---------------------------------------------------------------------------
# Celery (background jobs). Without a broker everything runs inline (eager),
# which keeps local development and the test suite dependency-free.
# ---------------------------------------------------------------------------
_BROKER = env('CELERY_BROKER_URL', REDIS_URL)
CELERY_TASK_ALWAYS_EAGER = TESTING or not _BROKER
CELERY_BROKER_URL = 'memory://' if CELERY_TASK_ALWAYS_EAGER else _BROKER
CELERY_RESULT_BACKEND = None
CELERY_TASK_EAGER_PROPAGATES = TESTING
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SERIALIZER = 'json'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_DEFAULT_QUEUE = 'default'
CELERY_TASK_ROUTES = {
    'notifications.*': {'queue': 'notifications'},
    'payments.*': {'queue': 'payments'},
}
CELERY_TIMEZONE = 'UTC'
CELERY_BEAT_SCHEDULE = {
    'elections-lifecycle-tick': {'task': 'elections.tasks.lifecycle_tick', 'schedule': 60.0},
    'elections-voting-reminders': {'task': 'elections.tasks.send_voting_reminders', 'schedule': crontab(minute=0)},
    'payments-reconcile': {'task': 'payments.tasks.reconcile_recent', 'schedule': crontab(minute='*/15')},
    'payments-expire-abandoned': {'task': 'payments.tasks.expire_abandoned', 'schedule': crontab(minute='*/10')},
    'fraud-anomaly-scan': {'task': 'fraud.tasks.anomaly_scan', 'schedule': crontab(minute='*/5')},
    'notifications-retry': {'task': 'notifications.tasks.retry_failed', 'schedule': crontab(minute='*/10')},
    'core-housekeeping': {'task': 'core.tasks.housekeeping', 'schedule': crontab(minute=30, hour=2)},
    'core-verify-audit-chain': {'task': 'core.tasks.verify_audit_chains', 'schedule': crontab(minute=0, hour='*/6')},
    'billing-generate-invoices': {'task': 'billing.tasks.generate_due_invoices', 'schedule': crontab(minute=15, hour=1)},
}
# Backpressure: when the queue backlog exceeds this many jobs, non-critical
# write endpoints shed load with 503 + Retry-After instead of piling on.
QUEUE_BACKPRESSURE_THRESHOLD = env_int('QUEUE_BACKPRESSURE_THRESHOLD', 50000)

# ---------------------------------------------------------------------------
# Authentication & passwords
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 10}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.Argon2PasswordHasher',
    'django.contrib.auth.hashers.PBKDF2PasswordHasher',
    'django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher',
    'django.contrib.auth.hashers.ScryptPasswordHasher',
]
if TESTING:
    PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/dashboard/'

# Account lockout / brute-force protection
LOGIN_MAX_FAILURES = env_int('LOGIN_MAX_FAILURES', 5)
LOGIN_LOCKOUT_SECONDS = env_int('LOGIN_LOCKOUT_SECONDS', 900)
# Voter-facing limits per client IP. Whole campuses often share one NAT
# address, so these are generous; per-voter limits stay strict.
VOTER_LOGIN_PER_IP_PER_MIN = env_int('VOTER_LOGIN_PER_IP_PER_MIN', 120)
VOTER_OTP_PER_IP_PER_5MIN = env_int('VOTER_OTP_PER_IP_PER_5MIN', 100)
VOTER_REGISTER_PER_IP_PER_10MIN = env_int('VOTER_REGISTER_PER_IP_PER_10MIN', 30)
# When True, staff and organization role holders must enrol a second factor
# (TOTP or passkey) before they can use the dashboard.
ENFORCE_STAFF_MFA = env_bool('ENFORCE_STAFF_MFA', False)

# WebAuthn / passkeys
WEBAUTHN_RP_ID = env('WEBAUTHN_RP_ID', 'localhost')
WEBAUTHN_RP_NAME = env('WEBAUTHN_RP_NAME', PLATFORM_NAME)
WEBAUTHN_ORIGIN = env('WEBAUTHN_ORIGIN', SITE_URL)

# OpenID Connect single sign-on providers (empty client id = disabled)
SSO_PROVIDERS = {
    'google': {
        'name': 'Google',
        'issuer': 'https://accounts.google.com',
        'client_id': env('GOOGLE_OIDC_CLIENT_ID'),
        'client_secret': env('GOOGLE_OIDC_CLIENT_SECRET'),
    },
    'microsoft': {
        'name': 'Microsoft',
        'issuer': f"https://login.microsoftonline.com/{env('MICROSOFT_OIDC_TENANT', 'common')}/v2.0",
        'client_id': env('MICROSOFT_OIDC_CLIENT_ID'),
        'client_secret': env('MICROSOFT_OIDC_CLIENT_SECRET'),
    },
}

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------
LANGUAGE_CODE = env('LANGUAGE_CODE', 'en')
LANGUAGES = [('en', 'English'), ('fr', 'Français')]
LOCALE_PATHS = [BASE_DIR / 'locale']
TIME_ZONE = env('TIME_ZONE', 'Africa/Accra')
USE_I18N = True
USE_TZ = True
DEFAULT_CURRENCY = env('DEFAULT_CURRENCY', 'GHS')
SUPPORTED_CURRENCIES = ['GHS', 'NGN', 'KES', 'ZAR', 'USD']

# ---------------------------------------------------------------------------
# Static & media
# ---------------------------------------------------------------------------
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'
# Private files (evidence, manifestos awaiting review, exports) never get a
# public URL - they are streamed only through permission-checked views.
PRIVATE_MEDIA_ROOT = Path(env('PRIVATE_MEDIA_ROOT', str(BASE_DIR / 'private_media')))

CLOUDINARY_STORAGE = {
    'CLOUD_NAME': env('CLOUDINARY_CLOUD_NAME'),
    'API_KEY': env('CLOUDINARY_API_KEY'),
    'API_SECRET': env('CLOUDINARY_API_SECRET'),
}
MEDIA_STORAGE = env('MEDIA_STORAGE', 'cloudinary' if (CLOUDINARY_STORAGE['CLOUD_NAME'] and not DEBUG) else 'local')
STORAGES = {
    'default': {
        'BACKEND': (
            'cloudinary_storage.storage.MediaCloudinaryStorage'
            if MEDIA_STORAGE == 'cloudinary' and not TESTING
            else 'django.core.files.storage.FileSystemStorage'
        ),
    },
    'staticfiles': {
        'BACKEND': (
            'django.contrib.staticfiles.storage.StaticFilesStorage'
            if (DEBUG or TESTING)
            else 'whitenoise.storage.CompressedManifestStaticFilesStorage'
        ),
    },
}
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 5000

# ---------------------------------------------------------------------------
# Payments (Paystack)
# ---------------------------------------------------------------------------
PAYSTACK_SECRET_KEY = env('PAYSTACK_SECRET_KEY')
PAYSTACK_PUBLIC_KEY = env('PAYSTACK_PUBLIC_KEY')
PAYSTACK_BASE_URL = env('PAYSTACK_BASE_URL', 'https://api.paystack.co')
# Local simulator for development without Paystack keys. Refused when DEBUG
# is off (see core.checks).
PAYMENTS_FAKE_GATEWAY = env_bool('PAYMENTS_FAKE_GATEWAY', DEBUG and not PAYSTACK_SECRET_KEY)
PAYMENT_ABANDON_AFTER_MINUTES = env_int('PAYMENT_ABANDON_AFTER_MINUTES', 30)
REFUND_DUAL_APPROVAL_THRESHOLD = env('REFUND_DUAL_APPROVAL_THRESHOLD', '500')

# Africa's Talking (SMS + USSD)
AT_USERNAME = env('AT_USERNAME', 'sandbox')
AT_API_KEY = env('AT_API_KEY')
AT_SENDER_ID = env('AT_SENDER_ID')
# Shared secret appended to the USSD callback URL (?token=...). Africa's
# Talking does not sign callbacks, so this plus an optional IP allow-list is
# what stops forged USSD sessions.
USSD_CALLBACK_TOKEN = env('USSD_CALLBACK_TOKEN')
USSD_ALLOWED_IPS = env_list('USSD_ALLOWED_IPS')

# WhatsApp Cloud API (optional notification channel)
WHATSAPP_TOKEN = env('WHATSAPP_TOKEN')
WHATSAPP_PHONE_NUMBER_ID = env('WHATSAPP_PHONE_NUMBER_ID')

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
EMAIL_HOST = env('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = env_int('EMAIL_PORT', 587)
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', True)
EMAIL_HOST_USER = env('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD')
EMAIL_TIMEOUT = 15
DEFAULT_FROM_EMAIL = env('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER or 'no-reply@flexyvotes.local')
SERVER_EMAIL = DEFAULT_FROM_EMAIL
# Published in /.well-known/security.txt - use a monitored inbox, not no-reply.
SECURITY_CONTACT = env('SECURITY_CONTACT', DEFAULT_FROM_EMAIL)
if TESTING:
    EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
elif EMAIL_HOST_USER and EMAIL_HOST_PASSWORD:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# ---------------------------------------------------------------------------
# Cryptography / key management
# ---------------------------------------------------------------------------
# Key-encryption keys for envelope encryption: "kid:base64-32-bytes,..."; the
# first entry encrypts new data keys, the rest are kept for decryption during
# rotation. Alternatively set KMS_KEY_ID to wrap data keys with AWS KMS.
FIELD_ENCRYPTION_KEYS = env('FIELD_ENCRYPTION_KEYS')
KMS_KEY_ID = env('KMS_KEY_ID')
AWS_REGION = env('AWS_REGION', 'eu-north-1')
# Ed25519 platform signing key (PEM or base64 raw) used for signed election
# configurations and certified results. Keep it in a secrets manager.
SIGNING_PRIVATE_KEY = env('SIGNING_PRIVATE_KEY')
# Comma-separated base64 public keys of retired signing keys (verification only).
SIGNING_PREVIOUS_PUBLIC_KEYS = env_list('SIGNING_PREVIOUS_PUBLIC_KEYS')
# Key for blind indexes (searchable hashes of encrypted columns). Rotating it
# requires `manage.py keys reindex`.
BLIND_INDEX_KEY = env('BLIND_INDEX_KEY')

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
# Number of reverse proxies (ALB, nginx) in front of the app. Used to pick
# the real client IP out of X-Forwarded-For; 0 = trust REMOTE_ADDR only.
TRUSTED_PROXY_COUNT = env_int('TRUSTED_PROXY_COUNT', 0)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = env_int('SESSION_COOKIE_AGE', 8 * 3600)
SESSION_COOKIE_NAME = 'fv_session'
CSRF_COOKIE_NAME = 'fv_csrftoken'
X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'
SECURE_CROSS_ORIGIN_OPENER_POLICY = 'same-origin'
CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS')
SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', False) and not DEBUG
SECURE_HSTS_SECONDS = 0 if DEBUG else env_int('SECURE_HSTS_SECONDS', 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0
SECURE_REDIRECT_EXEMPT = [r'^healthz/']

# Content-Security-Policy. Scripts must carry the per-request nonce; no
# inline event handlers are used anywhere in the templates.
CSP_REPORT_ONLY = env_bool('CSP_REPORT_ONLY', False)
CSP_EXTRA_SCRIPT_SRC = env_list('CSP_EXTRA_SCRIPT_SRC')
CSP_EXTRA_CONNECT_SRC = env_list('CSP_EXTRA_CONNECT_SRC')

# Cross-origin access to /api/ (e.g. a separate mobile/web client).
API_CORS_ALLOWED_ORIGINS = env_list('API_CORS_ALLOWED_ORIGINS')

# Outbound HTTP allow-list (SSRF protection). Integrations add their hosts.
OUTBOUND_HTTP_ALLOWED_HOSTS = env_list(
    'OUTBOUND_HTTP_ALLOWED_HOSTS',
    'api.paystack.co,accounts.google.com,www.googleapis.com,oauth2.googleapis.com,openidconnect.googleapis.com,'
    'login.microsoftonline.com,graph.facebook.com,api.africastalking.com,api.sandbox.africastalking.com,'
    'challenges.cloudflare.com,hcaptcha.com,api.hcaptcha.com,www.google.com',
)

# Bot protection. Provider: turnstile | hcaptcha | recaptcha | '' (honeypot only)
CAPTCHA_PROVIDER = env('CAPTCHA_PROVIDER', '')
CAPTCHA_SITE_KEY = env('CAPTCHA_SITE_KEY')
CAPTCHA_SECRET_KEY = env('CAPTCHA_SECRET_KEY')
# Minimum seconds between rendering and submitting a protected form.
FORM_MIN_FILL_SECONDS = 0 if TESTING else env_int('FORM_MIN_FILL_SECONDS', 2)

# ---------------------------------------------------------------------------
# Fraud engine thresholds (risk score 0-100)
# ---------------------------------------------------------------------------
FRAUD_MONITOR_THRESHOLD = env_int('FRAUD_MONITOR_THRESHOLD', 31)
FRAUD_CHALLENGE_THRESHOLD = env_int('FRAUD_CHALLENGE_THRESHOLD', 61)
FRAUD_HOLD_THRESHOLD = env_int('FRAUD_HOLD_THRESHOLD', 81)
FRAUD_FLAG_PROXIES = env_bool('FRAUD_FLAG_PROXIES', True)

# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------
BILLING_VAT_RATE = env('BILLING_VAT_RATE', '15.0')
BILLING_LEVY_RATE = env('BILLING_LEVY_RATE', '6.0')
BILLING_TRIAL_DAYS = env_int('BILLING_TRIAL_DAYS', 14)

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------
# Bearer token required to scrape /metrics (empty = staff session only).
METRICS_TOKEN = env('METRICS_TOKEN')
SENTRY_DSN = env('SENTRY_DSN')
OTEL_EXPORTER_OTLP_ENDPOINT = env('OTEL_EXPORTER_OTLP_ENDPOINT')
APP_VERSION = env('APP_VERSION', 'dev')
LOG_FORMAT = env('LOG_FORMAT', 'text')

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {
        'correlation': {'()': 'core.observability.CorrelationIdFilter'},
    },
    'formatters': {
        'json': {
            '()': 'pythonjsonlogger.json.JsonFormatter',
            'fmt': '%(asctime)s %(levelname)s %(name)s %(message)s %(correlation_id)s',
            'rename_fields': {'asctime': 'timestamp', 'levelname': 'level'},
        },
        'text': {
            'format': '%(asctime)s %(levelname)s [%(correlation_id)s] %(name)s: %(message)s',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'json' if LOG_FORMAT == 'json' else 'text',
            'filters': ['correlation'],
        },
    },
    'root': {'handlers': ['console'], 'level': env('DJANGO_LOG_LEVEL', 'WARNING' if TESTING else 'INFO')},
    'loggers': {
        'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False},
        'django.security': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
    },
}

if TESTING:
    TEST_RUNNER = 'core.testing.FlexyTestRunner'
    # Tests must never reach real third parties, whatever is in .env.
    PAYSTACK_SECRET_KEY = PAYSTACK_PUBLIC_KEY = None
    PAYMENTS_FAKE_GATEWAY = False
    AT_API_KEY = AT_SENDER_ID = USSD_CALLBACK_TOKEN = None
    USSD_ALLOWED_IPS = []
    WHATSAPP_TOKEN = WHATSAPP_PHONE_NUMBER_ID = None
    CAPTCHA_PROVIDER = CAPTCHA_SITE_KEY = CAPTCHA_SECRET_KEY = ''  # nosec B105
    KMS_KEY_ID = None
    SENTRY_DSN = OTEL_EXPORTER_OTLP_ENDPOINT = None
    METRICS_TOKEN = None
    SSO_PROVIDERS = {key: {**value, 'client_id': None, 'client_secret': None}  # nosec B105
                     for key, value in SSO_PROVIDERS.items()}

# Error tracking / tracing are wired up lazily so the packages stay optional.
from core.observability import configure_observability  # noqa: E402

configure_observability(
    sentry_dsn=SENTRY_DSN, otel_endpoint=OTEL_EXPORTER_OTLP_ENDPOINT,
    environment=env('ENVIRONMENT', 'development' if DEBUG else 'production'), release=APP_VERSION,
)
