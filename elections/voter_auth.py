"""Voter authentication.

Methods (enabled per election in ``Event.auth_methods``):

* CODE      - voter ID (when the election uses IDs) + one-time access code
* EMAIL_OTP - a 6-digit code to the email on the roll
* SMS_OTP   - a 6-digit code to the phone on the roll
* SSO       - institutional OpenID Connect (Google / Microsoft / custom)
* LDAP      - bind against the organization's LDAP / Active Directory
* ACCOUNT   - a signed-in platform account linked to the roll

``require_second_factor`` adds an email/SMS OTP after any primary method that
isn't itself an OTP. Errors are deliberately uniform so the login form can't
be used to enumerate the roll.
"""
import time

from django.utils import timezone

from core import audit, crypto, metrics, otp
from core.models import OTPChallenge
from voting.models import Event

from .models import Voter
from .voters import credential_matches

SESSION_KEY = 'fv_voter'
SESSION_TTL_SECONDS = 20 * 60
GENERIC_FAILURE = 'We could not verify those details. Check them and try again.'


class VoterAuthError(Exception):
    def __init__(self, message=GENERIC_FAILURE, code='invalid'):
        super().__init__(message)
        self.message = message
        self.code = code


def _check_status(voter):
    if voter.status == Voter.Status.VOTED:
        raise VoterAuthError('Our records show you have already voted in this election.', 'already_voted')
    if voter.status in (Voter.Status.SUSPENDED, Voter.Status.INELIGIBLE):
        raise VoterAuthError('Your voter record cannot vote. Please contact the election officials.', 'ineligible')
    return voter


def _fail(event, request, method, detail):
    metrics.LOGINS.labels(kind='voter', outcome='failure').inc()
    audit.record('VOTER_AUTH_FAILED', request=request, event=event, result='FAILURE',
                 summary=f'Voter sign-in failed ({method})', metadata={'method': method, 'detail': detail})
    raise VoterAuthError()


def authenticate_code(event, identifier, code, request=None):
    code = (code or '').strip().upper()
    identifier = (identifier or '').strip().upper()
    if not code:
        _fail(event, request, 'CODE', 'missing code')
    from voting.models import hash_voting_code

    voter = Voter.objects.filter(election=event, credential_hash=hash_voting_code(event.pk, code)).first()
    if voter is None:
        _fail(event, request, 'CODE', 'unknown code')
    if event.code_voting_mode == Event.CodeVotingMode.STUDENT_ID or voter.identifier:
        if event.code_voting_mode == Event.CodeVotingMode.STUDENT_ID and not identifier:
            _fail(event, request, 'CODE', 'missing identifier')
        if identifier and not crypto.constant_time_equals((voter.identifier or '').upper(), identifier):
            _fail(event, request, 'CODE', 'identifier mismatch')
    if not credential_matches(event, voter, code):
        _fail(event, request, 'CODE', 'hash mismatch')
    return _check_status(voter)


def find_voter(event, identifier_or_email):
    value = (identifier_or_email or '').strip()
    if not value:
        return None
    if '@' in value:
        return Voter.objects.filter(election=event, email_index=crypto.blind_index(value, 'email')).first()
    return Voter.objects.filter(election=event, identifier=value.upper()).first()


def start_otp(event, voter, channel, purpose='VOTER_LOGIN'):
    destination = voter.email if channel == OTPChallenge.Channel.EMAIL else voter.phone
    if not destination:
        raise VoterAuthError('No contact details on file for that method. Try another sign-in method.', 'no_contact')
    try:
        return otp.issue(purpose, 'voter', voter.pk, channel, destination, label=f'{event.title} sign-in',
                         event=event)
    except otp.OTPError as exc:
        raise VoterAuthError(str(exc), 'otp') from exc


def verify_otp(event, voter, challenge_id, code, purpose='VOTER_LOGIN'):
    try:
        challenge = otp.verify(challenge_id, code, purpose=purpose, subject_type='voter', subject_id=voter.pk)
    except otp.OTPError as exc:
        audit.record('VOTER_OTP_FAILED', event=event, target=voter, result='FAILURE', summary=str(exc))
        raise VoterAuthError(str(exc), 'otp') from exc
    now = timezone.now()
    if challenge.channel == OTPChallenge.Channel.EMAIL and not voter.email_verified_at:
        voter.email_verified_at = now
    if challenge.channel == OTPChallenge.Channel.SMS and not voter.phone_verified_at:
        voter.phone_verified_at = now
    if voter.status == Voter.Status.ELIGIBLE:
        voter.status = Voter.Status.VERIFIED
        voter.verified_at = now
    voter.save(update_fields=['email_verified_at', 'phone_verified_at', 'status', 'verified_at', 'updated_at'])
    return challenge


def authenticate_ldap(event, username, password, request=None):
    from core.ldap_auth import LDAPAuthError, ldap_authenticate

    organization = event.organization
    if organization is None or not organization.ldap_config:
        raise VoterAuthError('Directory sign-in is not configured for this election.', 'not_configured')
    try:
        attributes = ldap_authenticate(organization.ldap_config, username, password)
    except LDAPAuthError:
        _fail(event, request, 'LDAP', 'bind failed')
    identifier_attr = organization.ldap_config.get('identifier_attribute', 'uid')
    identifier = str(attributes.get(identifier_attr) or username).strip().upper()
    voter = Voter.objects.filter(election=event, identifier=identifier).first()
    if voter is None and attributes.get('mail'):
        voter = find_voter(event, attributes['mail'])
    if voter is None:
        _fail(event, request, 'LDAP', 'not on roll')
    return _check_status(voter)


def voter_from_sso(event, claims, request=None):
    """Match verified OIDC claims to the roll (by subject, then email); may
    self-register when the election allows it and the domain matches."""
    subject_index = crypto.blind_index(f"{claims.get('iss')}|{claims.get('sub')}", 'sso')
    voter = Voter.objects.filter(election=event, sso_subject_index=subject_index).first()
    email = (claims.get('email') or '').strip()
    if voter is None and email and claims.get('email_verified', False):
        voter = Voter.objects.filter(election=event, email_index=crypto.blind_index(email, 'email')).first()
        if voter is not None:
            voter.sso_subject_index = subject_index
            if not voter.email_verified_at:
                voter.email_verified_at = timezone.now()
            voter.save(update_fields=['sso_subject_index', 'email_verified_at', 'updated_at'])
    if voter is None and email and claims.get('email_verified') and event.allow_self_registration:
        from .voters import VoterImportError, self_register

        try:
            voter = self_register(event, identifier=None, email=email, full_name=claims.get('name', ''), request=request)
        except VoterImportError as exc:
            raise VoterAuthError(str(exc), 'registration') from exc
        voter.sso_subject_index = subject_index
        voter.source = Voter.Source.SSO
        voter.save(update_fields=['sso_subject_index', 'source'])
    if voter is None:
        _fail(event, request, 'SSO', 'not on roll')
    return _check_status(voter)


def voter_for_account(event, user, request=None):
    if not user.is_authenticated:
        raise VoterAuthError('Sign in to your account first.', 'login_required')
    voter = Voter.objects.filter(election=event, user=user).first()
    if voter is None and user.email:
        voter = Voter.objects.filter(election=event, email_index=crypto.blind_index(user.email, 'email')).first()
        if voter is not None and voter.user_id is None:
            voter.user = user
            voter.save(update_fields=['user', 'updated_at'])
    if voter is None:
        _fail(event, request, 'ACCOUNT', 'not on roll')
    return _check_status(voter)


# ---------------------------------------------------------------------------
# Voter session (short-lived, server-side)
# ---------------------------------------------------------------------------
def needs_second_factor(event, state):
    return bool(event.require_second_factor and not state.get('mfa')
                and state.get('method') not in ('EMAIL_OTP', 'SMS_OTP'))


def login_voter(request, event, voter, method, second_factor=False, request_audit=True):
    # New session key on every privilege change (session-fixation defence).
    request.session.cycle_key()
    sessions = request.session.get(SESSION_KEY, {})
    sessions[str(event.pk)] = {'voter': voter.pk, 'method': method, 'mfa': second_factor, 'at': int(time.time())}
    request.session[SESSION_KEY] = sessions
    metrics.LOGINS.labels(kind='voter', outcome='success').inc()
    if request_audit:
        audit.record('VOTER_AUTHENTICATED', request=request, event=event, target=voter,
                     summary=f'Voter {voter.identifier or voter.pk} signed in ({method})', metadata={'method': method})


def mark_second_factor(request, event):
    sessions = request.session.get(SESSION_KEY, {})
    state = sessions.get(str(event.pk))
    if state:
        state['mfa'] = True
        state['at'] = int(time.time())
        request.session[SESSION_KEY] = sessions


def current_voter(request, event):
    """(voter, state) for a live voter session on this election, else (None, None)."""
    state = request.session.get(SESSION_KEY, {}).get(str(event.pk))
    if not state or time.time() - state.get('at', 0) > SESSION_TTL_SECONDS:
        return None, None
    voter = Voter.objects.filter(pk=state['voter'], election=event).first()
    if voter is None:
        return None, None
    return voter, state


def logout_voter(request, event):
    sessions = request.session.get(SESSION_KEY, {})
    sessions.pop(str(event.pk), None)
    request.session[SESSION_KEY] = sessions
    for key in [k for k in request.session.keys() if k.startswith(f'fv_ballot_{event.pk}')]:
        del request.session[key]
