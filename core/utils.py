import ipaddress

from django.conf import settings
from django.utils.http import url_has_allowed_host_and_scheme

from .crypto import keyed_hash, random_token

DEVICE_COOKIE = 'fv_did'


def _valid_ip(value):
    try:
        return str(ipaddress.ip_address(value.strip()))
    except (ValueError, AttributeError):
        return None


def client_ip(request):
    """Real client IP. X-Forwarded-For is only honoured for the number of
    proxies we actually run behind (TRUSTED_PROXY_COUNT), so a client can't
    spoof its address to dodge rate limits or fraud rules."""
    if request is None:
        return None
    proxies = getattr(settings, 'TRUSTED_PROXY_COUNT', 0)
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if proxies and forwarded:
        hops = [h for h in (part.strip() for part in forwarded.split(',')) if h]
        if len(hops) >= proxies:
            ip = _valid_ip(hops[-proxies])
            if ip:
                return ip
    return _valid_ip(request.META.get('REMOTE_ADDR', '')) or None


def user_agent(request):
    if request is None:
        return ''
    return (request.META.get('HTTP_USER_AGENT') or '')[:300]


def device_id(request):
    """Stable random per-browser id (cookie set by UserPreferencesMiddleware)."""
    if request is None:
        return ''
    existing = getattr(request, '_fv_device_id', None) or request.COOKIES.get(DEVICE_COOKIE)
    if not existing or len(existing) > 64:
        existing = random_token(18)
        request._fv_new_device_id = existing
    request._fv_device_id = existing
    return existing


def device_hash(request):
    if request is None:
        return ''
    did = device_id(request)
    client_fp = ''
    if request.method == 'POST':
        client_fp = request.POST.get('fv_fp', '')
    client_fp = (client_fp or request.headers.get('X-Device-Fingerprint') or '')[:128]
    return keyed_hash('device', f'{did}|{client_fp}')


def mask_email(email):
    if not email or '@' not in email:
        return ''
    local, _, domain = email.partition('@')
    return local[:1] + '*' * max(len(local) - 1, 3) + '@' + domain


def mask_phone(phone):
    digits = ''.join(c for c in (phone or '') if c.isdigit() or c == '+')
    if len(digits) < 4:
        return ''
    return digits[:4] + '*' * max(len(digits) - 6, 2) + digits[-2:]


def normalize_phone(phone, default_country_code='233'):
    """Normalise to E.164-style digits with a leading '+' (Ghana default)."""
    if not phone:
        return ''
    raw = ''.join(c for c in str(phone) if c.isdigit() or c == '+')
    if raw.startswith('+'):
        return raw
    if raw.startswith('00'):
        return '+' + raw[2:]
    if raw.startswith('0') and len(raw) >= 10:
        return f'+{default_country_code}{raw[1:]}'
    if len(raw) >= 11:
        return '+' + raw
    return raw


def safe_next(request, candidate, default='/'):
    if candidate and url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()},
                                                     require_https=request.is_secure()):
        return candidate
    return default


def is_ajax(request):
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest' or \
        'application/json' in request.headers.get('Accept', '')
