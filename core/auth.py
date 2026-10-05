"""Staff / organizer authentication: lockout, MFA (TOTP, recovery codes,
passkeys), suspicious-login step-up and device tracking."""
import json
import time
from datetime import timedelta

import pyotp
from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.core.cache import cache
from django.utils import timezone

from . import audit, crypto, metrics
from .models import KnownDevice, UserSecurity, WebAuthnCredential
from .utils import client_ip, device_hash, user_agent

PENDING_KEY = 'fv_mfa_pending'
PENDING_TTL = 300
BACKEND = 'django.contrib.auth.backends.ModelBackend'


def security_for(user):
    security, _ = UserSecurity.objects.get_or_create(user=user)
    return security


# ---------------------------------------------------------------------------
# Lockout
# ---------------------------------------------------------------------------
def is_locked(username):
    user = get_user_model().objects.filter(username=username).first()
    if user is None:
        return False, None
    security = UserSecurity.objects.filter(user=user).first()
    if security and security.is_locked:
        return True, security.locked_until
    return False, None


def register_failure(username, request=None):
    user = get_user_model().objects.filter(username=username).first()
    if user is None:
        return
    security = security_for(user)
    security.failed_login_count += 1
    security.last_failed_at = timezone.now()
    maximum = settings.LOGIN_MAX_FAILURES
    if security.failed_login_count >= maximum:
        # Exponential back-off on repeated lockouts, capped at 24 hours.
        rounds = security.failed_login_count // maximum
        seconds = min(settings.LOGIN_LOCKOUT_SECONDS * (2 ** (rounds - 1)), 24 * 3600)
        security.locked_until = timezone.now() + timedelta(seconds=seconds)
        audit.record('ACCOUNT_LOCKED', request=request, actor=user, target=user, result='DENIED',
                     summary=f'Account locked for {seconds // 60} min after {security.failed_login_count} failures')
        from notifications.service import notify_user

        notify_user(user, 'security_alert', {
            'title': 'Your account was locked',
            'detail': f'{security.failed_login_count} failed sign-in attempts. The account is locked for '
                      f'{seconds // 60} minutes. If this was not you, reset your password.'}, channels=('EMAIL',))
    security.save()


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------
def mfa_methods(user):
    security = UserSecurity.objects.filter(user=user).first()
    methods = []
    if security and security.totp_enabled:
        methods.append('totp')
    if user.webauthn_credentials.exists():
        methods.append('passkey')
    if security and security.recovery_codes:
        methods.append('recovery')
    return methods


def suspicious_reasons(request, user):
    from fraud.engine import assess
    from fraud.models import FraudEvent

    reasons = []
    known = KnownDevice.objects.filter(user=user)
    if known.exists() and not known.filter(device_hash=device_hash(request)).exists():
        reasons.append('new device')
    security = UserSecurity.objects.filter(user=user).first()
    if security and security.failed_login_count >= 3:
        reasons.append('recent failed attempts')
    risk = assess(FraudEvent.Kind.STAFF_LOGIN, request=request, email=user.email, count_velocity=False,
                  record=False)
    if risk.score >= settings.FRAUD_CHALLENGE_THRESHOLD:
        reasons.append('high-risk connection')
    return reasons


def begin_login(request, user):
    """Password verified. Returns 'done' or 'mfa' (second step required)."""
    methods = mfa_methods(user)
    reasons = suspicious_reasons(request, user)
    if not methods and not (reasons and user.email):
        complete_login(request, user, 'password')
        return 'done'
    if not methods:
        # Step-up for an account without MFA: one-time code to their email.
        from . import otp

        try:
            challenge = otp.issue('STAFF_STEPUP', 'user', user.pk, 'EMAIL', user.email, label='sign-in verification')
            methods = ['email']
            request.session['fv_stepup_challenge'] = str(challenge.pk)
        except otp.OTPError:
            complete_login(request, user, 'password')
            return 'done'
    request.session[PENDING_KEY] = {'user': user.pk, 'at': int(time.time()), 'methods': methods, 'reasons': reasons}
    return 'mfa'


def pending_user(request):
    pending = request.session.get(PENDING_KEY)
    if not pending or time.time() - pending['at'] > PENDING_TTL:
        return None, None
    user = get_user_model().objects.filter(pk=pending['user'], is_active=True).first()
    return user, pending


def complete_login(request, user, method):
    request.session.pop(PENDING_KEY, None)
    request.session.pop('fv_stepup_challenge', None)
    login(request, user, backend=BACKEND)
    remember_device(request, user)
    audit.record('AUTH_METHOD', request=request, actor=user, target=user, summary=f'Signed in using {method}',
                 metadata={'method': method})


def remember_device(request, user):
    digest = device_hash(request)
    now = timezone.now()
    has_previous = KnownDevice.objects.filter(user=user).exists()
    device, created = KnownDevice.objects.get_or_create(user=user, device_hash=digest, defaults={
        'label': user_agent(request)[:200], 'ip_address': client_ip(request)})
    if not created:
        KnownDevice.objects.filter(pk=device.pk).update(last_seen_at=now, ip_address=client_ip(request))
    elif has_previous:
        from notifications.service import notify_user

        notify_user(user, 'security_alert', {
            'title': 'New sign-in to your account',
            'detail': f'A new device signed in from {client_ip(request) or "an unknown address"} '
                      f'({user_agent(request)[:120] or "unknown browser"}) at {now:%d %b %Y %H:%M %Z}.'})
        audit.record('AUTH_NEW_DEVICE', request=request, actor=user, target=user, summary='Sign-in from a new device')


# ---------------------------------------------------------------------------
# TOTP & recovery codes
# ---------------------------------------------------------------------------
def new_totp_secret():
    return pyotp.random_base32()


def totp_uri(user, secret):
    return pyotp.TOTP(secret).provisioning_uri(name=user.email or user.username, issuer_name=settings.PLATFORM_NAME)


def verify_totp(user, code, secret=None):
    security = security_for(user)
    secret = secret or security.totp_secret
    code = (code or '').strip().replace(' ', '')
    if not secret or not code.isdigit():
        return False
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    for offset in (-1, 0, 1):
        step = (now // 30) + offset
        if crypto.constant_time_equals(totp.at(step * 30), code):
            # Reject replay of an already-used code within its window.
            if not cache.add(f'totp-used:{user.pk}:{step}', 1, 120):
                return False
            return True
    return False


def generate_recovery_codes(user, count=10):
    codes = [f'{crypto.access_code(5)}-{crypto.access_code(5)}' for _ in range(count)]
    security = security_for(user)
    security.recovery_codes = [crypto.keyed_hash('recovery', c) for c in codes]
    security.save(update_fields=['recovery_codes'])
    audit.record('MFA_RECOVERY_CODES_GENERATED', actor=user, target=user, summary='Recovery codes regenerated')
    return codes


def use_recovery_code(user, code):
    security = security_for(user)
    digest = crypto.keyed_hash('recovery', (code or '').strip().upper())
    if digest in security.recovery_codes:
        security.recovery_codes = [c for c in security.recovery_codes if c != digest]
        security.save(update_fields=['recovery_codes'])
        audit.record('MFA_RECOVERY_CODE_USED', actor=user, target=user,
                     summary=f'Recovery code used ({len(security.recovery_codes)} left)')
        return True
    return False


# ---------------------------------------------------------------------------
# Passkeys (WebAuthn)
# ---------------------------------------------------------------------------
def _origins():
    return [settings.WEBAUTHN_ORIGIN]


def passkey_registration_options(request, user):
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement, UserVerificationRequirement)

    options = generate_registration_options(
        rp_id=settings.WEBAUTHN_RP_ID, rp_name=settings.WEBAUTHN_RP_NAME, user_name=user.username,
        user_id=str(user.pk).encode(), user_display_name=user.get_full_name() or user.username,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED, user_verification=UserVerificationRequirement.PREFERRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=crypto.b64d(c.credential_id))
                             for c in user.webauthn_credentials.all()],
    )
    request.session['fv_webauthn_reg'] = crypto.b64e(options.challenge)
    return options_to_json(options)


def passkey_register(request, user, credential, name='Passkey'):
    from webauthn import verify_registration_response

    challenge = request.session.pop('fv_webauthn_reg', None)
    if not challenge:
        raise ValueError('Registration session expired.')
    verified = verify_registration_response(credential=credential, expected_challenge=crypto.b64d(challenge),
                                            expected_rp_id=settings.WEBAUTHN_RP_ID, expected_origin=_origins())
    passkey = WebAuthnCredential.objects.create(
        user=user, credential_id=crypto.b64e(verified.credential_id), public_key=crypto.b64e(verified.credential_public_key),
        sign_count=verified.sign_count, name=(name or 'Passkey')[:80],
        transports=(json.loads(credential).get('response', {}).get('transports', []) if isinstance(credential, str) else []),
    )
    audit.record('MFA_PASSKEY_ADDED', request=request, actor=user, target=user, summary=f'Passkey "{passkey.name}" registered')
    return passkey


def passkey_authentication_options(request, user):
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement

    options = generate_authentication_options(
        rp_id=settings.WEBAUTHN_RP_ID, user_verification=UserVerificationRequirement.PREFERRED,
        allow_credentials=[PublicKeyCredentialDescriptor(id=crypto.b64d(c.credential_id))
                           for c in user.webauthn_credentials.all()],
    )
    request.session['fv_webauthn_auth'] = crypto.b64e(options.challenge)
    return options_to_json(options)


def passkey_authenticate(request, user, credential):
    from webauthn import verify_authentication_response
    from webauthn.helpers import parse_authentication_credential_json

    challenge = request.session.pop('fv_webauthn_auth', None)
    if not challenge:
        raise ValueError('Authentication session expired.')
    parsed = parse_authentication_credential_json(credential)
    stored = WebAuthnCredential.objects.filter(user=user, credential_id=crypto.b64e(parsed.raw_id)).first()
    if stored is None:
        raise ValueError('Unknown passkey.')
    verified = verify_authentication_response(
        credential=parsed, expected_challenge=crypto.b64d(challenge), expected_rp_id=settings.WEBAUTHN_RP_ID,
        expected_origin=_origins(), credential_public_key=crypto.b64d(stored.public_key),
        credential_current_sign_count=stored.sign_count)
    WebAuthnCredential.objects.filter(pk=stored.pk).update(sign_count=verified.new_sign_count, last_used_at=timezone.now())
    return stored


# ---------------------------------------------------------------------------
# API tokens
# ---------------------------------------------------------------------------
def create_api_token(user, name, organization=None, days=90):
    from .models import ApiToken

    raw = f'fv_{crypto.random_token(32)}'
    token = ApiToken.objects.create(user=user, organization=organization, name=name[:80], prefix=raw[:10],
                                    token_hash=crypto.sha256_hex(raw),
                                    expires_at=timezone.now() + timedelta(days=days) if days else None)
    audit.record('API_TOKEN_CREATED', actor=user, target=token, summary=f'API token "{name}" created')
    return raw, token


def authenticate_api_token(raw):
    from .models import ApiToken

    if not raw or not raw.startswith('fv_'):
        return None
    token = ApiToken.objects.select_related('user').filter(token_hash=crypto.sha256_hex(raw)).first()
    if token is None or not token.is_valid or not token.user.is_active:
        return None
    if not token.last_used_at or (timezone.now() - token.last_used_at).total_seconds() > 300:
        ApiToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
    metrics.LOGINS.labels(kind='api', outcome='success').inc()
    return token
