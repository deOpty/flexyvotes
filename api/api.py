"""FlexyVotes REST API v1 (OpenAPI docs at /api/v1/docs).

Authentication:
  * ``Authorization: Bearer fv_...``   personal API token (staff/organizers)
  * session cookie + CSRF token        the web console
  * ``Authorization: Bearer <ballot>`` voter ballot token (ballot/vote endpoints)

All POST operations that move money or record votes accept an
``Idempotency-Key`` header; repeating a request returns the original result.
Errors always use ``{"error": {"code", "message", "details", "correlation_id"}}``.
"""
from datetime import datetime
from decimal import Decimal
from functools import wraps
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from ninja import Field, NinjaAPI, Query, Router, Schema
from ninja.errors import AuthenticationError, HttpError, ValidationError
from ninja.pagination import PageNumberPagination, paginate
from ninja.security import APIKeyCookie, HttpBearer

from core import audit, auth as core_auth, crypto, idempotency, ratelimit
from core.models import AuditEvent, Organization
from core.observability import get_correlation_id
from core.rbac import check_perm, events_for_user, has_perm, organizations_for_user
from core.utils import client_ip, mask_email
from elections import lifecycle
from elections.ballot import BallotError, ballot_definition
from elections.casting import (AlreadyVoted, CastError, authorization_for, cast_ballot, issue_authorization)
from elections.models import Voter
from voting.models import Candidate, Category, Event


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
class TokenAuth(HttpBearer):
    def authenticate(self, request, token):
        api_token = core_auth.authenticate_api_token(token)
        if api_token is None:
            return None
        request.user = api_token.user
        request.api_token = api_token
        return api_token.user


class SessionAuth(APIKeyCookie):
    """Console session; CSRF is enforced for unsafe methods."""

    param_name = 'fv_session'

    def authenticate(self, request, key):
        if request.user.is_authenticated:
            return request.user
        return None


class BallotAuth(HttpBearer):
    def authenticate(self, request, token):
        authorization = authorization_for(token)
        if authorization is None:
            return None
        request.ballot_token = token
        request.authorization = authorization
        return authorization


staff_auth = [TokenAuth(), SessionAuth()]
optional_auth = [TokenAuth(), SessionAuth(), lambda request: True]

api = NinjaAPI(title='FlexyVotes API', version='1.0.0', urls_namespace='api-v1',
               description=__doc__, auth=staff_auth)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
def error(request, status, code, message, details=None):
    return api.create_response(request, {'error': {'code': code, 'message': message, 'details': details or {},
                                                   'correlation_id': get_correlation_id()}}, status=status)


@api.exception_handler(PermissionDenied)
def _forbidden(request, exc):
    return error(request, 403, 'forbidden', 'You do not have permission to perform this action.')


@api.exception_handler(Http404)
def _not_found(request, exc):
    return error(request, 404, 'not_found', 'Not found.')


@api.exception_handler(AuthenticationError)
def _unauthenticated(request, exc):
    return error(request, 401, 'unauthenticated', 'Authentication required.')


@api.exception_handler(ValidationError)
def _invalid(request, exc):
    return error(request, 422, 'validation_error', 'The request is invalid.', {'errors': exc.errors})


@api.exception_handler(HttpError)
def _http_error(request, exc):
    return error(request, exc.status_code, getattr(exc, 'code', 'error'), str(exc))


class ApiError(HttpError):
    def __init__(self, status, code, message):
        super().__init__(status, message)
        self.code = code


def limited(scope, limit, window=60):
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            principal = getattr(getattr(request, 'auth', None), 'pk', None) or client_ip(request) or 'anon'
            allowed, retry = ratelimit.hit(f'api:{scope}', str(principal), limit, window)
            if not allowed:
                response = error(request, 429, 'rate_limited', 'Too many requests.')
                response['Retry-After'] = str(retry)
                return response
            return func(request, *args, **kwargs)
        return wrapper
    return decorator


def idempotent(scope):
    """Replay the stored response for a repeated Idempotency-Key."""

    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            key = request.headers.get('Idempotency-Key')
            if not key:
                return func(request, *args, **kwargs)
            if not idempotency.valid_key(key):
                return error(request, 400, 'invalid_idempotency_key', 'Idempotency-Key must be 8-128 safe characters.')
            principal = getattr(request.user, 'pk', None) or getattr(getattr(request, 'authorization', None), 'pk', None) \
                or client_ip(request)
            payload = {'path': request.path, 'body': request.body.decode('utf-8', errors='replace')}
            try:
                record, replay = idempotency.begin(f'api:{scope}:{principal}', key, payload)
            except idempotency.IdempotencyConflict as exc:
                return error(request, 422, 'idempotency_conflict', str(exc))
            except idempotency.IdempotencyInProgress as exc:
                return error(request, 409, 'idempotency_in_progress', str(exc))
            if replay:
                response = api.create_response(request, record.response_body, status=record.response_status)
                response['Idempotent-Replay'] = 'true'
                return response
            try:
                result = func(request, *args, **kwargs)
            except Exception:
                idempotency.abandon(record)
                raise
            status, body = (result if isinstance(result, tuple) else (200, result))
            if isinstance(body, HttpResponse):
                idempotency.abandon(record)
                return body
            body = body.model_dump(mode='json') if hasattr(body, 'model_dump') else body
            idempotency.complete(record, status, body)
            return status, body
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ErrorOut(Schema):
    error: Dict[str, Any]


class UserOut(Schema):
    id: int
    username: str
    email: str
    is_platform_admin: bool
    organizations: List[Dict[str, Any]]


class TokenIn(Schema):
    username: str
    password: str
    otp: Optional[str] = None
    name: str = 'API token'


class TokenOut(Schema):
    token: str
    expires_at: Optional[datetime]


class OrganizationOut(Schema):
    id: int
    name: str
    slug: str
    kind: str
    default_timezone: str
    default_currency: str


class ElectionOut(Schema):
    id: int
    title: str
    description: str
    mode: str = Field(alias='voting_mode')
    status: str
    start_date: datetime
    end_date: datetime
    timezone: str
    currency: str
    organization_id: Optional[int]
    results_visibility: str
    accepting_votes: bool

    @staticmethod
    def resolve_accepting_votes(obj):
        return obj.accepting_votes()


class ElectionIn(Schema):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = ''
    mode: str = Field('INSTITUTIONAL', pattern='^(INSTITUTIONAL|PAID)$')
    start_date: datetime
    end_date: datetime
    timezone: str = 'Africa/Accra'
    currency: str = 'GHS'
    vote_price: Decimal = Decimal('1.00')
    organization_id: Optional[int] = None


class ElectionPatch(Schema):
    title: Optional[str] = None
    description: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    vote_price: Optional[Decimal] = None


class TransitionIn(Schema):
    action: str
    reason: str = ''


class CandidateOut(Schema):
    id: int
    name: str
    affiliation: str
    bio: str
    status: str
    position_id: Optional[int] = Field(None, alias='category_id')
    nominee_code: Optional[str]


class CandidateIn(Schema):
    name: str = Field(..., min_length=1, max_length=100)
    position_id: Optional[int] = None
    bio: str = ''
    manifesto: str = ''
    affiliation: str = ''
    email: str = ''


class PositionOut(Schema):
    id: int
    name: str
    ballot_type: str
    min_select: int
    max_select: int
    seats: int
    allow_abstain: bool


class PositionIn(Schema):
    name: str = Field(..., min_length=1, max_length=100)
    ballot_type: str = 'SINGLE'
    min_select: int = Field(1, ge=0)
    max_select: int = Field(1, ge=1)
    seats: int = Field(1, ge=1)
    max_score: int = Field(10, ge=1, le=100)
    allow_abstain: bool = True
    description: str = ''


class VoterOut(Schema):
    id: int
    identifier: Optional[str]
    status: str
    email_masked: str
    constituency_id: Optional[int]
    voted_at: Optional[datetime]

    @staticmethod
    def resolve_email_masked(obj):
        return mask_email(obj.email or '')


class VoterIn(Schema):
    identifier: Optional[str] = None
    full_name: str = ''
    email: Optional[str] = None
    phone: Optional[str] = None
    constituency: Optional[str] = None
    attributes: Dict[str, str] = {}


class ImportOut(Schema):
    created: int
    updated: int
    skipped: int
    codes_issued: int
    error_count: int
    errors: List[Dict[str, Any]]


class BallotSessionIn(Schema):
    identifier: Optional[str] = None
    code: str


class BallotSessionOut(Schema):
    ballot_token: str
    expires_at: datetime
    ballot: List[Dict[str, Any]]


class VoteIn(Schema):
    selections: Dict[str, Any]


class ReceiptOut(Schema):
    tracker: str
    cast_at: str
    election: str
    positions: int
    verify_url: str


class QuoteIn(Schema):
    candidate_id: int
    votes: Optional[int] = None
    package_id: Optional[int] = None
    discount_code: str = ''


class PaymentIn(QuoteIn):
    email: str = ''
    phone: str = ''
    name: str = ''


class PaymentOut(Schema):
    reference: str
    status: str
    amount: Decimal
    currency: str
    votes: int
    bonus_votes: int
    authorization_url: str
    votes_credited: bool
    held: bool


class AuditOut(Schema):
    id: int
    event_type: str
    actor_label: str
    election_id: Optional[int]
    target_type: str
    target_id: str
    summary: str
    result: str
    ip_address: Optional[str]
    correlation_id: str
    changes: Dict[str, Any]
    created_at: datetime
    hash: str


# ---------------------------------------------------------------------------
# Auth & users
# ---------------------------------------------------------------------------
auth_router = Router(tags=['auth'])


@auth_router.post('/token', auth=None, response={200: TokenOut, 401: ErrorOut, 429: ErrorOut})
@limited('token', 10)
def create_token(request, payload: TokenIn):
    from django.contrib.auth import authenticate

    locked, _ = core_auth.is_locked(payload.username)
    if locked:
        raise ApiError(401, 'locked', 'Account temporarily locked.')
    user = authenticate(request, username=payload.username, password=payload.password)
    if user is None:
        core_auth.register_failure(payload.username, request)
        raise ApiError(401, 'invalid_credentials', 'Invalid username or password.')
    methods = core_auth.mfa_methods(user)
    if 'totp' in methods and not core_auth.verify_totp(user, payload.otp or ''):
        raise ApiError(401, 'mfa_required', 'A valid authenticator code (otp) is required for this account.')
    raw, token = core_auth.create_api_token(user, payload.name)
    return {'token': raw, 'expires_at': token.expires_at}


@auth_router.delete('/token', response={204: None})
def revoke_token(request):
    token = getattr(request, 'api_token', None)
    if token is not None:
        from django.utils import timezone

        token.revoked_at = timezone.now()
        token.save(update_fields=['revoked_at'])
    return 204, None


def _me(request):
    user = request.user
    return {'id': user.pk, 'username': user.username, 'email': user.email,
            'is_platform_admin': user.is_staff or user.is_superuser,
            'organizations': [{'id': o.pk, 'name': o.name} for o in organizations_for_user(user)]}


@auth_router.get('/me', response=UserOut)
def me(request):
    return _me(request)


users_router = Router(tags=['users'])


@users_router.get('/me', response=UserOut)
def users_me(request):
    return _me(request)


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------
org_router = Router(tags=['organizations'])


@org_router.get('', response=List[OrganizationOut])
@paginate(PageNumberPagination, page_size=50)
def list_organizations(request):
    return organizations_for_user(request.user)


@org_router.get('/{org_id}', response=OrganizationOut)
def get_organization(request, org_id: int):
    return get_object_or_404(organizations_for_user(request.user), pk=org_id)


# ---------------------------------------------------------------------------
# Elections
# ---------------------------------------------------------------------------
election_router = Router(tags=['elections'])


def _visible_event(request, event_id, perm='election.view'):
    event = get_object_or_404(Event, pk=event_id)
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated and has_perm(user, perm, event):
        return event
    if perm == 'election.view' and event.is_public:
        return event
    raise Http404


@election_router.get('', response=List[ElectionOut], auth=optional_auth)
@paginate(PageNumberPagination, page_size=25)
def list_elections(request, status: Optional[str] = None, public: bool = False):
    if public or not request.user.is_authenticated:
        queryset = Event.objects.filter(is_active=True, status__in=Event.PUBLIC_STATUSES)
    else:
        queryset = events_for_user(request.user, 'election.view')
    if status:
        queryset = queryset.filter(status=status.upper())
    return queryset.order_by('-start_date')


@election_router.post('', response={201: ElectionOut, 403: ErrorOut})
def create_election(request, payload: ElectionIn):
    from core.tenancy import current_organization

    organization = Organization.objects.filter(pk=payload.organization_id).first() if payload.organization_id \
        else current_organization(request, 'election.create')
    check_perm(request.user, 'election.create', organization)
    if payload.end_date <= payload.start_date:
        raise ApiError(422, 'invalid_dates', 'end_date must be after start_date.')
    institutional = payload.mode == 'INSTITUTIONAL'
    event = Event.objects.create(
        title=payload.title, description=payload.description, organization=organization, organizer=request.user,
        voting_mode=Event.VotingMode.CODE_VOTING if institutional else Event.VotingMode.PAY_TO_VOTE,
        start_date=payload.start_date, end_date=payload.end_date, timezone=payload.timezone, currency=payload.currency,
        vote_price=Decimal('0') if institutional else payload.vote_price, platform_fee_percentage=Decimal('0') if institutional else Decimal('20'),
        results_visibility=Event.ResultsVisibility.AFTER_PUBLISH if institutional else Event.ResultsVisibility.LIVE,
        dual_approval_required=institutional)
    audit.record('ELECTION_CREATED', request=request, event=event, summary=f"Created election '{event.title}' via API")
    return 201, event


@election_router.get('/{event_id}', response=ElectionOut, auth=optional_auth)
def get_election(request, event_id: int):
    return lifecycle.tick(_visible_event(request, event_id))


@election_router.patch('/{event_id}', response=ElectionOut)
def update_election(request, event_id: int, payload: ElectionPatch):
    event = get_object_or_404(Event, pk=event_id)
    check_perm(request.user, 'election.edit', event)
    try:
        lifecycle.require_editable(event, 'config', request.user, request)
    except lifecycle.LifecycleError as exc:
        raise ApiError(409, 'not_editable', str(exc)) from exc
    fields = [k for k, v in payload.dict(exclude_unset=True).items()]
    before = audit.snapshot(event, fields)
    for name in fields:
        setattr(event, name, getattr(payload, name))
    if event.end_date <= event.start_date:
        raise ApiError(422, 'invalid_dates', 'end_date must be after start_date.')
    event.save()
    audit.record('ELECTION_CONFIG_UPDATED', request=request, event=event, summary='Updated via API',
                 changes=audit.diff(before, audit.snapshot(event, fields)))
    return event


@election_router.post('/{event_id}/transitions', response={200: ElectionOut, 409: ErrorOut})
def transition_election(request, event_id: int, payload: TransitionIn):
    event = get_object_or_404(Event, pk=event_id)
    try:
        return lifecycle.transition(event, payload.action, actor=request.user, request=request, reason=payload.reason)
    except lifecycle.LifecycleError as exc:
        raise ApiError(409, 'invalid_transition', str(exc)) from exc


@election_router.get('/{event_id}/positions', response=List[PositionOut], auth=optional_auth)
def list_positions(request, event_id: int):
    return _visible_event(request, event_id).categories.all()


@election_router.post('/{event_id}/positions', response={201: PositionOut, 409: ErrorOut})
def create_position(request, event_id: int, payload: PositionIn):
    event = get_object_or_404(Event, pk=event_id)
    check_perm(request.user, 'election.edit', event)
    if payload.ballot_type not in Category.BallotType.values:
        raise ApiError(422, 'invalid_ballot_type', f'ballot_type must be one of {Category.BallotType.values}')
    try:
        lifecycle.require_editable(event, 'ballot', request.user, request)
    except lifecycle.LifecycleError as exc:
        raise ApiError(409, 'not_editable', str(exc)) from exc
    position = Category.objects.create(event=event, display_order=event.categories.count(), **payload.dict())
    audit.record('ELECTION_POSITION_ADDED', request=request, event=event, target=position, summary=f"Position '{position.name}' added via API")
    return 201, position


@election_router.get('/{event_id}/candidates', response=List[CandidateOut], auth=optional_auth)
def list_candidates(request, event_id: int):
    return _visible_event(request, event_id).candidates.all()


@election_router.post('/{event_id}/candidates', response={201: CandidateOut, 409: ErrorOut})
def create_candidate(request, event_id: int, payload: CandidateIn):
    event = get_object_or_404(Event, pk=event_id)
    check_perm(request.user, 'candidate.create', event)
    try:
        lifecycle.require_editable(event, 'candidates', request.user, request)
    except lifecycle.LifecycleError as exc:
        raise ApiError(409, 'not_editable', str(exc)) from exc
    category = event.categories.filter(pk=payload.position_id).first() if payload.position_id else None
    if payload.position_id and category is None:
        raise ApiError(422, 'invalid_position', 'Unknown position.')
    candidate = Candidate.objects.create(event=event, category=category, name=payload.name, bio=payload.bio,
                                         manifesto=payload.manifesto, affiliation=payload.affiliation, email=payload.email)
    audit.record('ELECTION_CANDIDATE_ADDED', request=request, event=event, target=candidate, summary=f"Candidate '{candidate.name}' added via API")
    return 201, candidate


@election_router.get('/{event_id}/voters', response=List[VoterOut])
@paginate(PageNumberPagination, page_size=100)
def list_voters(request, event_id: int, status: Optional[str] = None):
    event = get_object_or_404(Event, pk=event_id)
    check_perm(request.user, 'voter.view', event)
    queryset = event.voters.order_by('pk')
    if status:
        queryset = queryset.filter(status=status.upper())
    return queryset


@election_router.post('/{event_id}/voters', response={200: ImportOut, 409: ErrorOut})
def import_voters(request, event_id: int, payload: List[VoterIn]):
    from elections import voters as voter_service

    event = get_object_or_404(Event, pk=event_id)
    if len(payload) > 10000:
        raise ApiError(413, 'too_many', 'At most 10,000 voters per request.')
    try:
        report = voter_service.import_voters(event, [v.dict() for v in payload], request.user, source=Voter.Source.API,
                                             request=request)
    except lifecycle.LifecycleError as exc:
        raise ApiError(409, 'not_editable', str(exc)) from exc
    except voter_service.VoterImportError as exc:
        raise ApiError(422, 'import_error', str(exc)) from exc
    return report.as_dict()


@election_router.get('/{event_id}/turnout', auth=optional_auth)
def turnout(request, event_id: int):
    from elections.voters import turnout as turnout_stats

    event = _visible_event(request, event_id)
    authorized = request.user.is_authenticated and has_perm(request.user, 'vote.view', event)
    if not authorized and event.status not in lifecycle.LOCKED_STATES:
        raise ApiError(403, 'not_public', 'Turnout is published after voting closes.')
    data = turnout_stats(event)
    if not authorized:
        data.pop('by_constituency', None)
    return data


@election_router.get('/{event_id}/results', auth=optional_auth)
def results(request, event_id: int):
    from elections import results as results_service

    event = _visible_event(request, event_id)
    public = results_service.public_results(event)
    if public is not None:
        body = {'certified': public['certified'], 'results': public['data']}
        if public['certification']:
            body['certification'] = {'signature': public['certification'].signature,
                                     'public_key': public['certification'].public_key,
                                     'payload': public['certification'].payload}
        return body
    if request.user.is_authenticated and has_perm(request.user, 'results.view', event):
        latest = event.results.exclude(status='SUPERSEDED').first()
        if latest:
            return {'certified': False, 'status': latest.status, 'results': latest.data}
    raise ApiError(404, 'not_available', 'Results are not available yet.')


@election_router.get('/{event_id}/verification', auth=optional_auth)
def verification(request, event_id: int):
    from elections.results import verification_bundle

    return verification_bundle(_visible_event(request, event_id))


# -- voter ballot ---------------------------------------------------------------
@election_router.post('/{event_id}/ballot/session', auth=None, response={200: BallotSessionOut, 401: ErrorOut, 409: ErrorOut})
@limited('ballot-session', settings.VOTER_LOGIN_PER_IP_PER_MIN)  # per IP; kiosks and campus NAT share one
def ballot_session(request, event_id: int, payload: BallotSessionIn):
    from elections import voter_auth

    event = get_object_or_404(Event, pk=event_id, voting_mode=Event.VotingMode.CODE_VOTING)
    event = lifecycle.tick(event)
    if 'CODE' not in (event.auth_methods or []) or event.require_second_factor:
        raise ApiError(409, 'unsupported_method', 'This election requires the web sign-in flow.')
    try:
        voter = voter_auth.authenticate_code(event, payload.identifier, payload.code, request)
        token, authorization = issue_authorization(event, voter, 'CODE', request)
    except voter_auth.VoterAuthError as exc:
        raise ApiError(401, exc.code, exc.message) from exc
    except CastError as exc:
        raise ApiError(409, exc.code, exc.message) from exc
    return {'ballot_token': token, 'expires_at': authorization.expires_at,
            'ballot': ballot_definition(event, authorization.ballot_style, include_bio=False)}


@election_router.get('/{event_id}/ballot', auth=BallotAuth(), response=List[Dict[str, Any]])
def get_ballot(request, event_id: int):
    authorization = request.auth
    if authorization.election_id != event_id:
        raise Http404
    return ballot_definition(authorization.election, authorization.ballot_style)


@election_router.post('/{event_id}/vote', auth=BallotAuth(), response={200: ReceiptOut, 409: ErrorOut, 422: ErrorOut})
@limited('vote', 10)
@idempotent('vote')
def vote(request, event_id: int, payload: VoteIn):
    authorization = request.auth
    if authorization.election_id != event_id:
        raise Http404
    try:
        receipt = cast_ballot(authorization.election, request.ballot_token, payload.selections, request=request)
    except AlreadyVoted as exc:
        raise ApiError(409, exc.code, exc.message) from exc
    except CastError as exc:
        raise ApiError(409, exc.code, exc.message) from exc
    except BallotError as exc:
        raise ApiError(422, 'invalid_ballot', exc.message) from exc
    return 200, {**receipt, 'verify_url': f'{settings.SITE_URL}/verify/{event_id}/?tracker={receipt["tracker"]}'}


# -- paid voting ---------------------------------------------------------------
@election_router.post('/{event_id}/payments/quote', auth=None)
def quote(request, event_id: int, payload: QuoteIn):
    from payments.service import quote as make_quote

    event = get_object_or_404(Event, pk=event_id, voting_mode=Event.VotingMode.PAY_TO_VOTE)
    candidate = get_object_or_404(Candidate, pk=payload.candidate_id, event=event)
    package = event.vote_packages.filter(pk=payload.package_id).first() if payload.package_id else None
    return make_quote(event, candidate, votes=payload.votes, package=package, discount_code=payload.discount_code).as_dict()


@election_router.post('/{event_id}/payments', auth=None, response={201: PaymentOut, 400: ErrorOut})
@limited('payment', 20)
@idempotent('payment')
def create_payment(request, event_id: int, payload: PaymentIn):
    from payments.service import PaymentError, initiate_vote_payment

    key = request.headers.get('Idempotency-Key')
    if not key:
        raise ApiError(400, 'idempotency_key_required', 'Send an Idempotency-Key header with payment requests.')
    event = get_object_or_404(Event, pk=event_id, voting_mode=Event.VotingMode.PAY_TO_VOTE)
    candidate = get_object_or_404(Candidate, pk=payload.candidate_id, event=event)
    package = event.vote_packages.filter(pk=payload.package_id).first() if payload.package_id else None
    try:
        payment = initiate_vote_payment(request, event, candidate, votes=payload.votes, package=package,
                                        discount_code=payload.discount_code, email=payload.email, phone=payload.phone,
                                        name=payload.name, idempotency_key=f'api:{key}')
    except PaymentError as exc:
        raise ApiError(400, exc.code, exc.message) from exc
    return 201, PaymentOut.from_orm(payment)


payments_router = Router(tags=['payments'])


@payments_router.get('/{reference}', auth=None, response=PaymentOut)
def payment_status(request, reference: str):
    from payments.models import Payment

    return get_object_or_404(Payment, reference=reference)


@payments_router.get('', response=List[PaymentOut])
@paginate(PageNumberPagination, page_size=50)
def list_payments(request, event_id: Optional[int] = None, status: Optional[str] = None):
    from payments.models import Payment

    queryset = Payment.objects.filter(event__in=events_for_user(request.user, 'payment.view'))
    if event_id:
        queryset = queryset.filter(event_id=event_id)
    if status:
        queryset = queryset.filter(status=status.upper())
    return queryset


webhooks_router = Router(tags=['webhooks'])


@webhooks_router.post('/paystack', auth=None)
def paystack_webhook(request):
    from payments.service import handle_webhook

    status, outcome = handle_webhook(request.body, request.headers.get('x-paystack-signature', ''))
    return api.create_response(request, {'outcome': outcome}, status=status)


audit_router = Router(tags=['audit'])


@audit_router.get('', response=List[AuditOut])
@paginate(PageNumberPagination, page_size=100)
def list_audit(request, election_id: Optional[int] = None, event_type: Optional[str] = None):
    if request.user.is_staff or request.user.is_superuser:
        queryset = AuditEvent.objects.all()
    else:
        orgs = organizations_for_user(request.user, 'audit.view')
        queryset = AuditEvent.objects.filter(organization_id__in=orgs.values('pk'))
        event_ids = events_for_user(request.user, 'audit.view').values_list('pk', flat=True)
        queryset = queryset | AuditEvent.objects.filter(election_id__in=list(event_ids))
    if election_id:
        queryset = queryset.filter(election_id=election_id)
    if event_type:
        queryset = queryset.filter(event_type__startswith=event_type.upper())
    return queryset.order_by('-created_at', '-seq')


@audit_router.get('/verify')
def verify_audit(request):
    from core.audit import chain_for, verify_chain

    chains = ['platform'] if (request.user.is_staff or request.user.is_superuser) else []
    chains += [chain_for(o.pk) for o in organizations_for_user(request.user, 'audit.view')]
    return {chain: dict(zip(('ok', 'checked', 'first_bad_seq', 'message'), verify_chain(chain))) for chain in chains}


api.add_router('/auth', auth_router)
api.add_router('/users', users_router)
api.add_router('/organizations', org_router)
api.add_router('/elections', election_router)
api.add_router('/payments', payments_router)
api.add_router('/webhooks', webhooks_router)
api.add_router('/audit', audit_router)
