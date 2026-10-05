"""Voter experience for institutional (secret-ballot) elections.

    Sign in / verify -> view ballot (candidate info) -> select -> review
    -> explicit confirmation -> cast -> confirmation with ballot tracker

Server-rendered so it works without JavaScript and on slow connections.
"""
import uuid

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core import captcha, crypto, ratelimit
from core.models import OTPChallenge
from core.sso import enabled_platform_providers
from core.utils import client_ip
from voting.models import Event

from . import lifecycle, voter_auth
from .ballot import BallotError, ballot_definition, describe_selections, rules_text
from .casting import AlreadyVoted, CastError, authorization_for, cast_ballot, issue_authorization, selections_from_post
from .models import VoteAuthorization, Voter

OTP_SESSION = 'fv_voter_otp'


def _event(event_id):
    event = get_object_or_404(Event.objects.select_related('organization'), pk=event_id)
    if not event.is_institutional or not event.is_public:
        raise Http404
    return lifecycle.tick(event)


def _keys(event):
    prefix = f'fv_ballot_{event.pk}'
    return {'token': f'{prefix}_token', 'selections': f'{prefix}_selections', 'submission': f'{prefix}_submission',
            'receipt': f'fv_receipt_{event.pk}'}


def _sso_options(event):
    options = []
    if 'SSO' not in (event.auth_methods or []):
        return options
    organization = event.organization
    if organization is not None and (organization.sso_config or {}).get('issuer'):
        options.append((f'org-{organization.pk}', (organization.sso_config or {}).get('name') or organization.name))
    options.extend(enabled_platform_providers())
    return options


def _after_primary_auth(request, event, voter, method):
    voter_auth.login_voter(request, event, voter, method,
                           second_factor=method in ('EMAIL_OTP', 'SMS_OTP'))
    _, state = voter_auth.current_voter(request, event)
    if voter_auth.needs_second_factor(event, state):
        channel = OTPChallenge.Channel.EMAIL if voter.email else OTPChallenge.Channel.SMS
        try:
            challenge = voter_auth.start_otp(event, voter, channel, purpose='VOTER_2FA')
        except voter_auth.VoterAuthError as exc:
            voter_auth.logout_voter(request, event)
            messages.error(request, exc.message)
            return redirect('elections:vote_start', event_id=event.pk)
        request.session[OTP_SESSION] = {'event': event.pk, 'voter': voter.pk, 'challenge': str(challenge.pk),
                                        'purpose': 'VOTER_2FA', 'hint': challenge.destination_hint, 'channel': channel}
        return redirect('elections:vote_otp', event_id=event.pk)
    return redirect('elections:vote_ballot', event_id=event.pk)


def vote_start(request, event_id):
    event = _event(event_id)
    voter, state = voter_auth.current_voter(request, event)
    if voter is not None and not voter_auth.needs_second_factor(event, state) and voter.can_vote             and event.accepting_votes():
        return redirect('elections:vote_ballot', event_id=event.pk)
    methods = event.auth_methods or ['CODE']
    context = {'event': event, 'methods': methods, 'sso_options': _sso_options(event),
               'student_id_mode': event.code_voting_mode == Event.CodeVotingMode.STUDENT_ID,
               'receipt': request.session.get(_keys(event)['receipt'])}
    if request.method != 'POST':
        return render(request, 'vote/start.html', context)

    action = request.POST.get('action', 'code')
    ip = client_ip(request) or 'unknown'
    # Per IP: generous by default, because a whole campus can share one NAT
    # address. Access codes carry ~49 bits, so this limit is not what stops guessing.
    allowed, retry = ratelimit.hit('voter-login', ip, settings.VOTER_LOGIN_PER_IP_PER_MIN, 60)
    # Per identifier: stops guessing one voter's code. Only when an identifier
    # is given - in code-only elections it is blank and would otherwise make
    # every voter share a single counter.
    identifier = (request.POST.get('identifier') or request.POST.get('username') or '').strip().upper()
    allowed_ident = True
    if identifier:
        ident_key = crypto.sha256_hex(f'{event.pk}:{identifier}')
        allowed_ident, _ = ratelimit.hit('voter-login-ident', ident_key, 10, 600)
    if not (allowed and allowed_ident):
        return ratelimit.too_many_requests(request, retry, 'Too many sign-in attempts. Please wait and try again.')
    human, _ = captcha.verify_human(request)
    if not human:
        messages.error(request, 'Please complete the verification step and try again.')
        return render(request, 'vote/start.html', context, status=400)
    try:
        if action == 'code' and 'CODE' in methods:
            voter = voter_auth.authenticate_code(event, request.POST.get('identifier'), request.POST.get('code'), request)
            return _after_primary_auth(request, event, voter, 'CODE')
        if action == 'ldap' and 'LDAP' in methods:
            voter = voter_auth.authenticate_ldap(event, request.POST.get('username'), request.POST.get('password'), request)
            return _after_primary_auth(request, event, voter, 'LDAP')
        if action == 'account' and 'ACCOUNT' in methods:
            voter = voter_auth.voter_for_account(event, request.user, request)
            return _after_primary_auth(request, event, voter, 'ACCOUNT')
        if action == 'otp' and ({'EMAIL_OTP', 'SMS_OTP'} & set(methods)):
            voter = voter_auth.find_voter(event, request.POST.get('identifier'))
            channel = OTPChallenge.Channel.SMS if request.POST.get('channel') == 'SMS' and 'SMS_OTP' in methods \
                else OTPChallenge.Channel.EMAIL
            if channel == OTPChallenge.Channel.EMAIL and 'EMAIL_OTP' not in methods:
                channel = OTPChallenge.Channel.SMS
            if voter is None or not voter.can_vote:
                # Same response whether or not the voter exists (no enumeration).
                request.session[OTP_SESSION] = {'event': event.pk, 'voter': None, 'challenge': str(uuid.uuid4()),
                                                'purpose': 'VOTER_LOGIN', 'hint': '', 'channel': channel}
                return redirect('elections:vote_otp', event_id=event.pk)
            challenge = voter_auth.start_otp(event, voter, channel, purpose='VOTER_LOGIN')
            request.session[OTP_SESSION] = {'event': event.pk, 'voter': voter.pk, 'challenge': str(challenge.pk),
                                            'purpose': 'VOTER_LOGIN', 'hint': challenge.destination_hint,
                                            'channel': channel}
            return redirect('elections:vote_otp', event_id=event.pk)
        messages.error(request, 'That sign-in method is not available for this election.')
    except voter_auth.VoterAuthError as exc:
        messages.error(request, exc.message)
    return render(request, 'vote/start.html', context, status=400)


def vote_otp(request, event_id):
    event = _event(event_id)
    pending = request.session.get(OTP_SESSION)
    if not pending or pending.get('event') != event.pk:
        return redirect('elections:vote_start', event_id=event.pk)
    context = {'event': event, 'hint': pending.get('hint'), 'channel': pending.get('channel'),
               'second_factor': pending['purpose'] == 'VOTER_2FA'}
    if request.method != 'POST':
        return render(request, 'vote/otp.html', context)
    allowed, retry = ratelimit.hit('voter-otp', client_ip(request) or 'unknown', settings.VOTER_OTP_PER_IP_PER_5MIN, 300)
    if not allowed:
        return ratelimit.too_many_requests(request, retry)
    voter = Voter.objects.filter(pk=pending.get('voter'), election=event).first() if pending.get('voter') else None
    if request.POST.get('action') == 'resend':
        if voter is not None:
            try:
                challenge = voter_auth.start_otp(event, voter, pending['channel'], purpose=pending['purpose'])
                pending['challenge'] = str(challenge.pk)
                request.session[OTP_SESSION] = pending
                messages.success(request, 'A new code has been sent.')
            except voter_auth.VoterAuthError as exc:
                messages.error(request, exc.message)
        else:
            messages.success(request, 'A new code has been sent.')
        return redirect('elections:vote_otp', event_id=event.pk)
    if voter is None:
        messages.error(request, voter_auth.GENERIC_FAILURE)
        return render(request, 'vote/otp.html', context, status=400)
    try:
        voter_auth.verify_otp(event, voter, pending['challenge'], request.POST.get('code'), purpose=pending['purpose'])
    except voter_auth.VoterAuthError as exc:
        messages.error(request, exc.message)
        return render(request, 'vote/otp.html', context, status=400)
    request.session.pop(OTP_SESSION, None)
    if pending['purpose'] == 'VOTER_2FA':
        voter_auth.mark_second_factor(request, event)
        return redirect('elections:vote_ballot', event_id=event.pk)
    return _after_primary_auth(request, event, voter, 'EMAIL_OTP' if pending['channel'] == 'EMAIL' else 'SMS_OTP')


def _require_voter(request, event):
    voter, state = voter_auth.current_voter(request, event)
    if voter is None:
        messages.error(request, 'Your session has ended. Please sign in again.')
        return None
    if voter_auth.needs_second_factor(event, state):
        return None
    return voter


def _authorization(request, event, voter):
    keys = _keys(event)
    token = request.session.get(keys['token'])
    authorization = authorization_for(token)
    if authorization is not None and authorization.voter_id == voter.pk and \
            authorization.status == VoteAuthorization.Status.ISSUED:
        from django.utils import timezone

        if authorization.expires_at > timezone.now():
            return token, authorization
    token, authorization = issue_authorization(event, voter, (voter_auth.current_voter(request, event)[1] or {}).get('method', 'CODE'),
                                               request)
    request.session[keys['token']] = token
    return token, authorization


def vote_ballot(request, event_id):
    event = _event(event_id)
    voter = _require_voter(request, event)
    if voter is None:
        return redirect('elections:vote_start', event_id=event.pk)
    keys = _keys(event)
    try:
        token, authorization = _authorization(request, event, voter)
    except AlreadyVoted:
        voter_auth.logout_voter(request, event)
        return redirect('elections:vote_receipt', event_id=event.pk)
    except CastError as exc:
        voter_auth.logout_voter(request, event)
        messages.error(request, exc.message)
        return redirect('elections:vote_start', event_id=event.pk)
    definition = ballot_definition(event, authorization.ballot_style)
    for position in definition:
        position['rules'] = rules_text(position)
    selections = request.session.get(keys['selections']) or {}
    errors = {}
    if request.method == 'POST':
        try:
            payload = selections_from_post(event, authorization.ballot_style, request.POST)
            from .ballot import validate_ballot

            validate_ballot(event, authorization.ballot_style, payload)
            request.session[keys['selections']] = payload
            request.session[keys['submission']] = uuid.uuid4().hex
            return redirect('elections:vote_review', event_id=event.pk)
        except BallotError as exc:
            errors[getattr(exc.position, 'pk', None)] = exc.message
            messages.error(request, exc.message)
            selections = {}
    return render(request, 'vote/ballot.html', {
        'event': event, 'voter': voter, 'positions': definition, 'selections': selections, 'errors': errors,
        'expires_at': authorization.expires_at, 'preview': False,
    })


def vote_review(request, event_id):
    event = _event(event_id)
    voter = _require_voter(request, event)
    keys = _keys(event)
    if voter is None:
        return redirect('elections:vote_start', event_id=event.pk)
    payload = request.session.get(keys['selections'])
    token = request.session.get(keys['token'])
    if payload is None or not token:
        return redirect('elections:vote_ballot', event_id=event.pk)
    authorization = authorization_for(token)
    if authorization is None or authorization.voter_id != voter.pk:
        return redirect('elections:vote_ballot', event_id=event.pk)
    rows = describe_selections(event, _normalized(event, authorization.ballot_style, payload))
    context = {'event': event, 'voter': voter, 'rows': rows, 'submission_id': request.session.get(keys['submission'])}
    if request.method != 'POST':
        return render(request, 'vote/review.html', context)
    if request.POST.get('action') == 'edit':
        return redirect('elections:vote_ballot', event_id=event.pk)
    if request.POST.get('confirm') != 'on':
        messages.error(request, 'Tick the confirmation box to cast your ballot.')
        return render(request, 'vote/review.html', context, status=400)
    if request.POST.get('submission_id') != request.session.get(keys['submission']):
        # Stale tab / replayed form: show the outcome instead of re-submitting.
        return redirect('elections:vote_receipt', event_id=event.pk)
    try:
        receipt = cast_ballot(event, token, payload, request=request)
    except AlreadyVoted:
        voter_auth.logout_voter(request, event)
        messages.info(request, 'Your ballot was already recorded.')
        return redirect('elections:vote_receipt', event_id=event.pk)
    except (CastError, BallotError) as exc:
        messages.error(request, getattr(exc, 'message', str(exc)))
        return redirect('elections:vote_ballot', event_id=event.pk)
    request.session[keys['receipt']] = receipt
    voter_auth.logout_voter(request, event)
    return redirect('elections:vote_receipt', event_id=event.pk)


def _normalized(event, style, payload):
    from .ballot import validate_ballot

    try:
        return validate_ballot(event, style, payload)
    except BallotError:
        return {}


def vote_receipt(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    receipt = request.session.get(_keys(event)['receipt'])
    return render(request, 'vote/receipt.html', {'event': event, 'receipt': receipt,
                                                 'verify_url': reverse('elections:verify', args=[event.pk])})


@require_POST
def vote_logout(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    voter_auth.logout_voter(request, event)
    messages.info(request, 'You have signed out of the ballot.')
    return redirect('event_detail', event_id=event.pk)


REG_SESSION = 'fv_voter_registration'


def register(request, event_id):
    """Self-registration (institutional email domain + email verification)."""
    from core import otp

    from .voters import VoterImportError, self_register

    event = _event(event_id)
    if not event.allow_self_registration:
        raise Http404
    pending = request.session.get(REG_SESSION)
    context = {'event': event, 'domains': event.email_domain_list, 'pending': pending if pending and pending.get('event') == event.pk else None}
    if request.method != 'POST':
        return render(request, 'vote/register.html', context)
    allowed, retry = ratelimit.hit('voter-register', client_ip(request) or 'unknown',
                                    settings.VOTER_REGISTER_PER_IP_PER_10MIN, 600)
    if not allowed:
        return ratelimit.too_many_requests(request, retry)
    if request.POST.get('action') == 'verify' and context['pending']:
        pending = context['pending']
        try:
            otp.verify(pending['challenge'], request.POST.get('code'), purpose='VOTER_REGISTRATION',
                       subject_type='registration', subject_id=pending['subject'])
            voter = self_register(event, identifier=pending['identifier'], email=pending['email'],
                                  full_name=pending['name'], request=request)
        except (otp.OTPError, VoterImportError) as exc:
            messages.error(request, str(exc))
            return render(request, 'vote/register.html', context, status=400)
        request.session.pop(REG_SESSION, None)
        messages.success(request, 'You are registered. Your access details have been emailed to you.')
        if event.accepting_votes():
            voter_auth.login_voter(request, event, voter, 'EMAIL_OTP', second_factor=True)
            return redirect('elections:vote_ballot', event_id=event.pk)
        return redirect('event_detail', event_id=event.pk)
    human, _ = captcha.verify_human(request)
    email = (request.POST.get('email') or '').strip().lower()
    identifier = (request.POST.get('identifier') or '').strip()
    name = (request.POST.get('name') or '').strip()[:150]
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    try:
        validate_email(email)
    except ValidationError:
        human = False
    domains = event.email_domain_list
    if not human or (domains and email.rsplit('@', 1)[-1] not in domains):
        messages.error(request, 'Enter your institutional email address' + (f' (@{", @".join(domains)})' if domains else '') + '.')
        return render(request, 'vote/register.html', context, status=400)
    subject = crypto.sha256_hex(f'{event.pk}:{email}')[:32]
    try:
        challenge = otp.issue('VOTER_REGISTRATION', 'registration', subject, 'EMAIL', email,
                              label=f'{event.title} registration', event=event)
    except otp.OTPError as exc:
        messages.error(request, str(exc))
        return render(request, 'vote/register.html', context, status=400)
    request.session[REG_SESSION] = {'event': event.pk, 'email': email, 'identifier': identifier, 'name': name,
                                    'challenge': str(challenge.pk), 'subject': subject,
                                    'hint': challenge.destination_hint}
    return redirect('elections:vote_register', event_id=event.pk)
