"""Bot protection for public forms.

Always on: a honeypot field plus a signed render timestamp (bots that post
instantly or fill hidden fields are rejected). Optionally a real CAPTCHA
(Cloudflare Turnstile, hCaptcha or reCAPTCHA) when keys are configured.
"""
import logging
import time

from django.conf import settings
from django.core import signing

from .http import safe_request
from .utils import client_ip

logger = logging.getLogger(__name__)

HONEYPOT_FIELD = 'website'
TIMESTAMP_FIELD = 'fv_ts'
_SALT = 'flexyvotes.formstamp'

PROVIDERS = {
    'turnstile': {
        'script': 'https://challenges.cloudflare.com/turnstile/v0/api.js',
        'widget_class': 'cf-turnstile',
        'response_field': 'cf-turnstile-response',
        'verify_url': 'https://challenges.cloudflare.com/turnstile/v0/siteverify',
    },
    'hcaptcha': {
        'script': 'https://js.hcaptcha.com/1/api.js',
        'widget_class': 'h-captcha',
        'response_field': 'h-captcha-response',
        'verify_url': 'https://api.hcaptcha.com/siteverify',
    },
    'recaptcha': {
        'script': 'https://www.google.com/recaptcha/api.js',
        'widget_class': 'g-recaptcha',
        'response_field': 'g-recaptcha-response',
        'verify_url': 'https://www.google.com/recaptcha/api/siteverify',
    },
}


def provider_config():
    name = (settings.CAPTCHA_PROVIDER or '').lower()
    if name in PROVIDERS and settings.CAPTCHA_SITE_KEY and settings.CAPTCHA_SECRET_KEY:
        return name, PROVIDERS[name]
    return None, None


def form_stamp():
    return signing.dumps(int(time.time()), salt=_SALT)


def verify_human(request, require_captcha=False):
    """Return (ok, reason). ``require_captcha`` forces the CAPTCHA challenge
    (used when the fraud engine asks for one)."""
    if request.POST.get(HONEYPOT_FIELD):
        return False, 'honeypot'
    stamp = request.POST.get(TIMESTAMP_FIELD)
    if stamp is not None:
        try:
            rendered = signing.loads(stamp, salt=_SALT, max_age=6 * 3600)
        except signing.BadSignature:
            return False, 'bad-stamp'
        if time.time() - rendered < settings.FORM_MIN_FILL_SECONDS:
            return False, 'too-fast'
    name, config = provider_config()
    if not config:
        return True, 'no-captcha-configured'
    token = request.POST.get(config['response_field'], '')
    if not token:
        return (False, 'captcha-missing') if require_captcha or name else (True, 'ok')
    try:
        response = safe_request('POST', config['verify_url'], integration='captcha', timeout=8, data={
            'secret': settings.CAPTCHA_SECRET_KEY, 'response': token, 'remoteip': client_ip(request) or '',
        })
        ok = bool(response.json().get('success'))
    except Exception:  # noqa: BLE001 - fail closed on provider errors
        logger.warning('CAPTCHA verification failed', exc_info=True)
        return False, 'captcha-error'
    return ok, 'ok' if ok else 'captcha-failed'
