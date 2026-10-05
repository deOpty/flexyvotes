"""Deployment safety checks (run by `manage.py check --deploy` and at startup)."""
from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


@register(Tags.security, deploy=True)
def platform_security_checks(app_configs, **kwargs):
    issues = []
    if settings.DEBUG:
        return issues
    if settings.PAYMENTS_FAKE_GATEWAY:
        issues.append(Error('PAYMENTS_FAKE_GATEWAY must never be enabled in production.', id='flexyvotes.E001'))
    if not (settings.FIELD_ENCRYPTION_KEYS or settings.KMS_KEY_ID):
        issues.append(Warning(
            'No FIELD_ENCRYPTION_KEYS or KMS_KEY_ID set: data keys are wrapped with a key derived from SECRET_KEY.',
            hint='Generate one with `manage.py keys generate-kek` and store it in your secrets manager.',
            id='flexyvotes.W001'))
    if not settings.SIGNING_PRIVATE_KEY:
        issues.append(Warning(
            'SIGNING_PRIVATE_KEY is not set: result/config signatures use a key derived from SECRET_KEY.',
            hint='Generate one with `manage.py keys generate-signing-key`.', id='flexyvotes.W002'))
    if not settings.BLIND_INDEX_KEY:
        issues.append(Warning('BLIND_INDEX_KEY is not set (derived from SECRET_KEY).', id='flexyvotes.W003'))
    if not settings.PAYSTACK_SECRET_KEY:
        issues.append(Warning('PAYSTACK_SECRET_KEY is not set: paid voting is unavailable.', id='flexyvotes.W004'))
    if not settings.REDIS_URL:
        issues.append(Warning('REDIS_URL is not set: rate limits are per-process and jobs run inline.',
                              id='flexyvotes.W005'))
    if settings.USSD_CALLBACK_TOKEN is None and not settings.USSD_ALLOWED_IPS:
        issues.append(Warning('USSD callback is unauthenticated (set USSD_CALLBACK_TOKEN or USSD_ALLOWED_IPS).',
                              id='flexyvotes.W006'))
    if getattr(settings, 'MEDIA_STORAGE_MISCONFIGURED', False):
        issues.append(Warning('MEDIA_STORAGE=cloudinary but CLOUDINARY_CLOUD_NAME / _API_KEY / _API_SECRET are not all '
                              'set: uploads are stored on local disk instead.', id='flexyvotes.W007'))
    return issues
