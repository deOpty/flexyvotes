"""Account security: second-factor login, TOTP, recovery codes, passkeys,
sessions/devices, API tokens and SSO."""
import base64
import io
import json

import qrcode
from django.contrib import messages
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.decorators import login_required
from django.contrib.sessions.models import Session
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import audit, auth, otp, ratelimit, sso
from .models import ApiToken, KnownDevice, UserSession, WebAuthnCredential
from .utils import client_ip, safe_next


def _next(request):
    return safe_next(request, request.session.get('fv_login_next'), reverse('dashboard'))


@ratelimit.ratelimit('mfa', 10, 60, key='ip')
def mfa(request):
    user, pending = auth.pending_user(request)
    if user is None:
        messages.error(request, 'Your sign-in session expired. Please sign in again.')
        return redirect('login')
    if request.method == 'POST':
        method = request.POST.get('method')
        code = (request.POST.get('code') or '').strip()
        ok = False
        if method == 'totp' and 'totp' in pending['methods']:
            ok = auth.verify_totp(user, code)
        elif method == 'recovery' and 'recovery' in pending['methods']:
            ok = auth.use_recovery_code(user, code)
        elif method == 'email' and 'email' in pending['methods']:
            try:
                otp.verify(request.session.get('fv_stepup_challenge'), code, purpose='STAFF_STEPUP',
                           subject_type='user', subject_id=user.pk)
                ok = True
            except otp.OTPError as exc:
                messages.error(request, str(exc))
        if ok:
            next_url = _next(request)
            auth.complete_login(request, user, method)
            return redirect(next_url)
        auth.register_failure(user.username, request)
        audit.record('AUTH_MFA_FAILED', request=request, actor=user, target=user, result='FAILURE',
                     summary=f'Second factor ({method}) failed')
        if not messages.get_messages(request):
            messages.error(request, 'That code was not accepted.')
    return render(request, 'account/mfa.html', {'methods': pending['methods'], 'reasons': pending.get('reasons', []),
                                                'username': user.username})


@require_POST
def mfa_passkey_options(request):
    user, pending = auth.pending_user(request)
    if user is None or 'passkey' not in pending['methods']:
        return JsonResponse({'error': 'expired'}, status=400)
    return JsonResponse(json.loads(auth.passkey_authentication_options(request, user)))


@require_POST
def mfa_passkey_verify(request):
    user, pending = auth.pending_user(request)
    if user is None:
        return JsonResponse({'error': 'expired'}, status=400)
    try:
        auth.passkey_authenticate(request, user, request.body.decode('utf-8'))
    except Exception:  # noqa: BLE001 - any verification failure
        auth.register_failure(user.username, request)
        return JsonResponse({'error': 'Passkey verification failed.'}, status=400)
    next_url = _next(request)
    auth.complete_login(request, user, 'passkey')
    return JsonResponse({'redirect': next_url})


def _qr_data_uri(text):
    image = qrcode.make(text)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()


@login_required
def security(request):
    user = request.user
    security_row = auth.security_for(user)
    context = {'security': security_row, 'passkeys': user.webauthn_credentials.all(),
               'sessions': UserSession.objects.filter(user=user, ended_at__isnull=True, revoked=False),
               'devices': KnownDevice.objects.filter(user=user).order_by('-last_seen_at'),
               'tokens': ApiToken.objects.filter(user=user, revoked_at__isnull=True),
               'current_session': request.session.session_key, 'enrol': request.GET.get('enrol')}
    pending_secret = request.session.get('fv_totp_setup')
    if pending_secret:
        context['totp_setup'] = {'secret': pending_secret, 'qr': _qr_data_uri(auth.totp_uri(user, pending_secret))}
    new_codes = request.session.pop('fv_new_recovery_codes', None)
    if new_codes:
        context['recovery_codes'] = new_codes
    new_token = request.session.pop('fv_new_api_token', None)
    if new_token:
        context['new_token'] = new_token
    return render(request, 'account/security.html', context)


@login_required
@require_POST
def security_action(request):
    user = request.user
    action = request.POST.get('action')
    if action == 'totp_start':
        request.session['fv_totp_setup'] = auth.new_totp_secret()
    elif action == 'totp_confirm':
        secret = request.session.get('fv_totp_setup')
        if secret and auth.verify_totp(user, request.POST.get('code'), secret=secret):
            security_row = auth.security_for(user)
            security_row.totp_secret = secret
            security_row.totp_confirmed_at = timezone.now()
            security_row.save()
            request.session.pop('fv_totp_setup', None)
            request.session['fv_new_recovery_codes'] = auth.generate_recovery_codes(user)
            audit.record('MFA_TOTP_ENABLED', request=request, target=user, summary='Authenticator app enabled')
            messages.success(request, 'Authenticator app enabled. Save your recovery codes now.')
        else:
            messages.error(request, 'That code did not match. Check the time on your phone and try again.')
    elif action == 'totp_disable':
        if not user.check_password(request.POST.get('password') or ''):
            messages.error(request, 'Enter your current password to turn off the authenticator app.')
        else:
            security_row = auth.security_for(user)
            security_row.totp_secret = None
            security_row.totp_confirmed_at = None
            security_row.save()
            audit.record('MFA_TOTP_DISABLED', request=request, target=user, summary='Authenticator app disabled')
            messages.success(request, 'Authenticator app removed.')
    elif action == 'recovery_regenerate':
        request.session['fv_new_recovery_codes'] = auth.generate_recovery_codes(user)
    elif action == 'passkey_delete':
        passkey = get_object_or_404(WebAuthnCredential, pk=request.POST.get('passkey'), user=user)
        audit.record('MFA_PASSKEY_REMOVED', request=request, target=user, summary=f'Passkey "{passkey.name}" removed')
        passkey.delete()
    elif action in ('session_revoke', 'sessions_revoke_others'):
        sessions = UserSession.objects.filter(user=user, ended_at__isnull=True, revoked=False)
        if action == 'session_revoke':
            sessions = sessions.filter(pk=request.POST.get('session'))
        else:
            sessions = sessions.exclude(session_key=request.session.session_key)
        keys = list(sessions.values_list('session_key', flat=True))
        Session.objects.filter(session_key__in=keys).delete()
        sessions.update(revoked=True, ended_at=timezone.now())
        audit.record('AUTH_SESSIONS_REVOKED', request=request, target=user, summary=f'{len(keys)} session(s) signed out')
        messages.success(request, f'{len(keys)} session(s) signed out.')
        if request.session.session_key in keys:
            logout(request)
            return redirect('login')
    elif action == 'device_forget':
        KnownDevice.objects.filter(user=user, pk=request.POST.get('device')).delete()
    elif action == 'token_create':
        from billing.service import feature_enabled
        from .tenancy import current_organization

        organization = current_organization(request, 'election.view')
        if organization is not None and not feature_enabled(organization, 'api'):
            messages.error(request, 'API access requires the Professional plan or above.')
        else:
            raw, _ = auth.create_api_token(user, (request.POST.get('name') or 'API token')[:80], organization)
            request.session['fv_new_api_token'] = raw
    elif action == 'token_revoke':
        token = get_object_or_404(ApiToken, pk=request.POST.get('token'), user=user)
        token.revoked_at = timezone.now()
        token.save(update_fields=['revoked_at'])
        audit.record('API_TOKEN_REVOKED', request=request, target=token, summary=f'API token "{token.name}" revoked')
    return redirect('account:security')


@login_required
@require_POST
def passkey_register_options(request):
    return JsonResponse(json.loads(auth.passkey_registration_options(request, request.user)))


@login_required
@require_POST
def passkey_register(request):
    try:
        body = json.loads(request.body)
        passkey = auth.passkey_register(request, request.user, json.dumps(body['credential']), body.get('name', 'Passkey'))
    except Exception:  # noqa: BLE001
        return JsonResponse({'error': 'Passkey registration failed.'}, status=400)
    return JsonResponse({'ok': True, 'id': passkey.pk})


# ---------------------------------------------------------------------------
# Single sign-on
# ---------------------------------------------------------------------------
def sso_start(request, provider):
    from .models import Organization

    allowed, retry = ratelimit.hit('sso-start', client_ip(request) or 'unknown', 20, 60)
    if not allowed:
        return ratelimit.too_many_requests(request, retry)
    organization = None
    if provider.startswith('org-'):
        organization = Organization.objects.filter(pk=provider[4:] if provider[4:].isdigit() else 0).first()
    event_id = request.GET.get('event')
    purpose = 'voter' if event_id else 'staff'
    try:
        url = sso.build_authorize_url(request, provider, purpose=purpose, organization=organization,
                                      event_id=int(event_id) if event_id and event_id.isdigit() else None,
                                      next_url=safe_next(request, request.GET.get('next'), '/'))
    except (sso.SSOError, Exception) as exc:  # noqa: BLE001 - provider down / misconfigured
        messages.error(request, str(exc) if isinstance(exc, sso.SSOError) else 'Single sign-on is unavailable right now.')
        return redirect(f'/e/{event_id}/vote/' if event_id else 'login')
    return redirect(url)


def sso_callback(request, provider):
    try:
        claims, flow = sso.handle_callback(request, provider)
    except sso.SSOError as exc:
        audit.record('SSO_FAILED', request=request, result='FAILURE', summary=str(exc), metadata={'provider': provider})
        messages.error(request, str(exc))
        return redirect('login')
    except Exception:  # noqa: BLE001
        messages.error(request, 'Single sign-on failed. Please try again.')
        return redirect('login')
    if flow['purpose'] == 'voter' and flow.get('event'):
        from elections import voter_auth
        from elections.views_voter import _after_primary_auth
        from voting.models import Event

        event = get_object_or_404(Event, pk=flow['event'])
        try:
            voter = voter_auth.voter_from_sso(event, claims, request)
        except voter_auth.VoterAuthError as exc:
            messages.error(request, exc.message)
            return redirect('elections:vote_start', event_id=event.pk)
        return _after_primary_auth(request, event, voter, 'SSO')
    email = (claims.get('email') or '').strip()
    user = get_user_model().objects.filter(email__iexact=email, is_active=True).first() \
        if email and claims.get('email_verified') else None
    if user is None:
        audit.record('SSO_NO_ACCOUNT', request=request, result='FAILURE', summary='SSO sign-in with no linked account',
                     metadata={'provider': provider})
        messages.error(request, 'No organizer account is linked to that identity.')
        return redirect('login')
    request.session['fv_login_next'] = flow.get('next') or reverse('dashboard')
    if auth.begin_login(request, user) == 'mfa':
        return redirect('account:mfa')
    return redirect(_next(request))
