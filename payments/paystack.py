"""Paystack API client.

All calls go through core.http.safe_request (allow-listed host, timeouts,
circuit breaker). With PAYMENTS_FAKE_GATEWAY (development only - refused by
the deploy checks when DEBUG is off) a local simulator stands in for Paystack
so the complete pay -> webhook -> verify -> credit flow can be exercised
without real keys.
"""
import hashlib
import hmac
import logging
from datetime import timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache

from core.http import CircuitOpen, safe_request

logger = logging.getLogger(__name__)

# Public, well-known key of the local simulator (never valid at Paystack).
FAKE_SECRET = 'sk_test_flexyvotes_local_simulator'  # nosec B105


class PaystackError(Exception):
    """``str(exc)`` is safe to show to payers. ``detail`` holds the provider's
    own reason (e.g. "Invalid key") for logs and the payment's history."""

    def __init__(self, message, detail=''):
        super().__init__(message)
        self.detail = detail or message


UNAVAILABLE = 'Online payment is temporarily unavailable. Please try again in a few minutes.'


def fake_mode():
    return bool(settings.PAYMENTS_FAKE_GATEWAY and settings.DEBUG) or bool(
        settings.PAYMENTS_FAKE_GATEWAY and getattr(settings, 'TESTING', False))


def secret_key():
    return FAKE_SECRET if fake_mode() else (settings.PAYSTACK_SECRET_KEY or '')


def configured():
    return fake_mode() or bool(settings.PAYSTACK_SECRET_KEY)


def _request(method, path, **kwargs):
    if not settings.PAYSTACK_SECRET_KEY:
        raise PaystackError(UNAVAILABLE, 'PAYSTACK_SECRET_KEY is not set.')
    url = f'{settings.PAYSTACK_BASE_URL.rstrip("/")}{path}'
    try:
        response = safe_request(method, url, integration='paystack', timeout=20,
                                headers={'Authorization': f'Bearer {settings.PAYSTACK_SECRET_KEY}',
                                         'Content-Type': 'application/json'}, **kwargs)
    except CircuitOpen as exc:
        raise PaystackError(UNAVAILABLE, 'Paystack circuit breaker is open after repeated failures.') from exc
    except Exception as exc:  # noqa: BLE001 - network failure
        logger.warning('Paystack request failed: %s %s (%s)', method, path, exc.__class__.__name__)
        raise PaystackError('Could not reach the payment provider. Please check your connection and try again.',
                            f'Network error talking to Paystack: {exc.__class__.__name__}') from exc
    try:
        body = response.json()
    except ValueError as exc:
        logger.warning('Paystack returned a non-JSON response: %s %s (HTTP %s)', method, path, response.status_code)
        raise PaystackError(UNAVAILABLE, f'Non-JSON response from Paystack (HTTP {response.status_code}).') from exc
    if not isinstance(body, dict):
        body = {}
    if response.status_code >= 400 or not body.get('status'):
        reason = str(body.get('message') or f'HTTP {response.status_code}')[:300]
        if response.status_code in (401, 403):
            # A wrong, revoked or test/live-mismatched key. Payers can do nothing about it; operators must.
            logger.error('Paystack rejected the API key on %s %s (HTTP %s: %s). Check PAYSTACK_SECRET_KEY.',
                         method, path, response.status_code, reason)
            raise PaystackError(UNAVAILABLE, f'Paystack rejected the secret key (HTTP {response.status_code}): {reason}')
        logger.warning('Paystack %s %s failed (HTTP %s): %s', method, path, response.status_code, reason)
        if response.status_code >= 500:
            raise PaystackError(UNAVAILABLE, f'Paystack server error (HTTP {response.status_code}): {reason}')
        # 4xx validation problems (amount, email, currency, channel) describe the request itself.
        raise PaystackError(f'The payment could not be started: {reason}', reason)
    return body


# ---------------------------------------------------------------------------
# Local simulator
# ---------------------------------------------------------------------------
def _fake_key(reference):
    return f'fakepay:{reference}'


def fake_store(reference):
    return cache.get(_fake_key(reference))


def fake_set(reference, data):
    cache.set(_fake_key(reference), data, 24 * 3600)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def initialize(*, email, amount_minor, currency, reference, callback_url, metadata, channels=None, base_url=None):
    """``base_url``: the public origin the payer is on (core.utils.public_base_url);
    used for the simulator's checkout link. Defaults to SITE_URL."""
    if fake_mode():
        from django.urls import reverse

        fake_set(reference, {'status': 'ongoing', 'amount': amount_minor, 'currency': currency, 'email': email,
                             'metadata': metadata, 'reference': reference})
        origin = (base_url or settings.SITE_URL).rstrip('/')
        return {'authorization_url': f"{origin}{reverse('payments:simulator', args=[reference])}",
                'access_code': f'fake_{reference}', 'reference': reference}
    payload = {'email': email, 'amount': amount_minor, 'currency': currency, 'reference': reference,
               'callback_url': callback_url, 'metadata': metadata}
    if channels:
        payload['channels'] = channels
    return _request('POST', '/transaction/initialize', json=payload)['data']


def verify(reference):
    if fake_mode():
        data = fake_store(reference)
        if data is None:
            raise PaystackError('Transaction reference not found.')
        return {'status': data['status'], 'reference': reference, 'amount': data['amount'], 'currency': data['currency'],
                'channel': data.get('channel', 'card'), 'id': abs(hash(reference)) % 10 ** 9,
                'ip_address': data.get('ip', '127.0.0.1'), 'fees': int(data['amount'] * 0.0195),
                'paid_at': data.get('paid_at'), 'metadata': data.get('metadata', {}),
                'authorization': {'signature': data.get('card_signature', 'SIG_FAKE_CARD'), 'last4': '4081',
                                  'bank': 'Test Bank', 'brand': 'visa', 'country_code': data.get('country', 'GH')},
                'customer': {'email': data.get('email', '')}}
    return _request('GET', f'/transaction/verify/{reference}')['data']


def list_transactions(start, end, page=1, per_page=100):
    if fake_mode():
        return [], {'pageCount': 1}
    params = {'from': start.astimezone(dt_timezone.utc).isoformat(), 'to': end.astimezone(dt_timezone.utc).isoformat(),
              'page': page, 'perPage': per_page}
    body = _request('GET', '/transaction', params=params)
    return body.get('data', []), body.get('meta', {})


def refund(reference, amount_minor=None):
    if fake_mode():
        return {'id': f'rf_{reference}', 'status': 'pending', 'transaction': {'reference': reference}}
    payload = {'transaction': reference}
    if amount_minor:
        payload['amount'] = amount_minor
    return _request('POST', '/refund', json=payload)['data']


def charge_mobile_money(*, email, amount_minor, currency, phone, provider, reference, metadata):
    if fake_mode():
        fake_set(reference, {'status': 'pay_offline', 'amount': amount_minor, 'currency': currency, 'email': email,
                             'metadata': metadata, 'channel': 'mobile_money'})
        return {'status': 'pay_offline', 'reference': reference, 'display_text': 'Approve the prompt on your phone.'}
    payload = {'email': email, 'amount': amount_minor, 'currency': currency, 'reference': reference,
               'metadata': metadata, 'mobile_money': {'phone': phone, 'provider': provider}}
    return _request('POST', '/charge', json=payload)['data']


def signature_for(body: bytes) -> str:
    return hmac.new(secret_key().encode('utf-8'), body, hashlib.sha512).hexdigest()


def verify_signature(body: bytes, signature: str) -> bool:
    if not secret_key() or not signature:
        return False
    return hmac.compare_digest(signature_for(body), signature)


GH_PROVIDERS = {
    'mtn': ('024', '025', '053', '054', '055', '059'),
    'vod': ('020', '050'),
    'atl': ('026', '027', '056', '057'),
}


def ghana_momo_provider(phone):
    digits = ''.join(c for c in phone if c.isdigit())
    if digits.startswith('233'):
        digits = '0' + digits[3:]
    for provider, prefixes in GH_PROVIDERS.items():
        if digits[:3] in prefixes:
            return provider, digits
    return None, digits
