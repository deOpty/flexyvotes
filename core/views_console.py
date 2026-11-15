"""Platform / organization console pages."""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import audit, tenancy
from .models import (AccessRequest, AuditChainHead, AuditEvent, Organization, Role, RoleAssignment, SupportMessage,
                     SupportTicket)
from .rbac import (APPROVAL_QUEUE_PERMISSIONS, NON_REQUESTABLE_ROLES, ORGANIZER_PERMISSIONS, PERMISSIONS,
                   events_for_user, has_perm, is_console_user, is_platform_admin, organizations_for_user,
                   permission_required, permissions_anywhere)


def _require_console(request):
    if not is_console_user(request.user):
        messages.error(request, 'Your organizer account is pending admin approval.')
        return False
    return True


@login_required
def dashboard(request):
    from elections.models import ApprovalRequest, Voter
    from fraud.models import FraudEvent
    from payments.models import Payment
    from voting.models import Event, VoteTransaction

    if not _require_console(request):
        return redirect('home')
    now = timezone.now()
    today = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    platform_admin = is_platform_admin(request.user)
    perms = permissions_anywhere(request.user)
    events = events_for_user(request.user, 'election.view').select_related('organization')
    paid_today = VoteTransaction.objects.filter(candidate__event__in=events, status='Success', created_at__gte=today)
    stats = {
        'active': events.filter(status=Event.Status.OPEN).count(),
        'votes_today': (paid_today.aggregate(n=Sum('number_of_votes'))['n'] or 0)
        + Voter.objects.filter(election__in=events, voted_at__gte=today).count(),
        'approvals': ApprovalRequest.objects.filter(status=ApprovalRequest.Status.PENDING,
                                                    election__in=events_for_user(request.user, 'approval.decide')).exclude(
            requested_by=request.user).count(),
        'in_review': events_for_user(request.user, 'election.review').filter(status=Event.Status.REVIEW).count(),
    }
    # Figures behind permissions the user lacks are neither computed nor shown.
    if 'payment.view' in perms:
        revenue_events = events_for_user(request.user, 'payment.view')
        revenue_30 = Payment.objects.filter(event__in=revenue_events, status='SUCCESS',
                                            created_at__gte=now - timedelta(days=30)).aggregate(total=Sum('amount'))['total'] \
            or Decimal('0')
        legacy_revenue = VoteTransaction.objects.filter(
            candidate__event__in=revenue_events, status='Success', payment__isnull=True,
            created_at__gte=now - timedelta(days=30)).aggregate(t=Sum('amount'))['t'] or 0
        stats['revenue_30d'] = revenue_30 + legacy_revenue
    if 'voter.view' in perms:
        stats['voters'] = Voter.objects.filter(election__in=events_for_user(request.user, 'voter.view')).count()
    if 'fraud.view' in perms:
        fraud_scope = Q(event__in=events_for_user(request.user, 'fraud.view'))
        if platform_admin:
            fraud_scope |= Q(event__isnull=True)
        stats['fraud_alerts'] = FraudEvent.objects.filter(fraud_scope, status=FraudEvent.Status.OPEN).count()
    logs = []
    if platform_admin:
        logs = AuditEvent.objects.all()[:12]
    elif 'audit.view' in perms:
        logs = AuditEvent.objects.filter(
            organization_id__in=organizations_for_user(request.user, 'audit.view').values('pk'))[:12]
    status_counts = dict(events.values_list('status').annotate(n=Count('pk')))
    notifications = request.user.notifications.filter(channel='IN_APP', read_at__isnull=True)[:10]
    return render(request, 'console/dashboard.html', {
        'stats': stats, 'events': events.order_by('-created_at')[:50], 'status_counts': status_counts,
        'logs': logs, 'notifications': notifications, 'organization': tenancy.current_organization(request),
        'organizations': organizations_for_user(request.user)[:50], 'currency': settings.DEFAULT_CURRENCY,
        'is_organizer_only': not platform_admin and not (perms - ORGANIZER_PERMISSIONS),
    })


@login_required
@require_POST
def switch_organization(request):
    org = tenancy.switch_organization(request, request.POST.get('organization'))
    if org:
        messages.success(request, f'Now working in {org.name}.')
    return redirect('dashboard')


@login_required
def approvals(request):
    from elections import integrity
    from elections.models import ApprovalRequest

    if not (permissions_anywhere(request.user) & APPROVAL_QUEUE_PERMISSIONS
            or ApprovalRequest.objects.filter(requested_by=request.user).exists()):
        raise PermissionDenied
    scope = Q(election__in=events_for_user(request.user, 'election.view'))
    if is_platform_admin(request.user):
        scope |= Q(election__isnull=True)
    queryset = ApprovalRequest.objects.filter(scope).select_related('election', 'requested_by', 'decided_by')
    if request.method == 'POST':
        req = get_object_or_404(queryset, pk=request.POST.get('request'))
        try:
            if request.POST.get('decision') == 'cancel' and req.requested_by_id == request.user.pk \
                    and req.status == ApprovalRequest.Status.PENDING:
                req.status = ApprovalRequest.Status.CANCELLED
                req.save(update_fields=['status'])
                audit.record(f'APPROVAL_{req.action}_CANCELLED', request=request, event=req.election, target=req)
                messages.success(request, 'Request cancelled.')
            else:
                req = integrity.decide(req, request.user, request.POST.get('decision') == 'approve',
                                       (request.POST.get('note') or '')[:1000], request)
                messages.success(request, f'Request {req.get_status_display().lower()}.')
        except PermissionDenied:
            messages.error(request, 'You are not allowed to decide this request.')
        except integrity.IntegrityControlError as exc:
            messages.error(request, str(exc))
        return redirect('console:approvals')
    return render(request, 'console/approvals.html', {
        'pending': queryset.filter(status=ApprovalRequest.Status.PENDING),
        'history': queryset.exclude(status=ApprovalRequest.Status.PENDING)[:100]})


@login_required
def audit_log(request):
    if is_platform_admin(request.user):
        entries = AuditEvent.objects.all()
        chains = AuditChainHead.objects.all()
    else:
        orgs = organizations_for_user(request.user, 'audit.view')
        if not orgs.exists():
            raise PermissionDenied
        entries = AuditEvent.objects.filter(organization_id__in=orgs.values('pk'))
        chains = AuditChainHead.objects.filter(chain__in=[f'org:{o.pk}' for o in orgs])
    event_type = (request.GET.get('event_type') or '').strip().upper()
    if event_type:
        entries = entries.filter(event_type__startswith=event_type)
    actor = (request.GET.get('actor_label') or '').strip()
    if actor:
        entries = entries.filter(actor_label__icontains=actor)
    result = (request.GET.get('result') or '').strip().upper()
    if result in AuditEvent.Result.values:
        entries = entries.filter(result=result)
    if (request.GET.get('election') or '').isdigit():
        entries = entries.filter(election_id=request.GET['election'])
    verification = None
    if request.GET.get('verify'):
        from .audit import verify_chain

        verification = {head.chain: verify_chain(head.chain) for head in chains}
        audit.record('AUDIT_CHAIN_VERIFIED', request=request, summary='Audit chains verified on demand',
                     metadata={c: r[0] for c, r in verification.items()})
    return render(request, 'console/audit.html', {'page': Paginator(entries, 50).get_page(request.GET.get('page')),
                                                  'filters': request.GET, 'verification': verification,
                                                  'chains': chains})


@login_required
def team(request, org_id=None):
    organization = get_object_or_404(Organization, pk=org_id) if org_id else tenancy.current_organization(request, 'org.manage')
    if organization is None or not has_perm(request.user, 'org.manage', organization):
        raise PermissionDenied
    User = get_user_model()
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'grant':
                identifier = (request.POST.get('user') or '').strip()
                user = User.objects.filter(Q(username=identifier) | Q(email__iexact=identifier)).first() if identifier else None
                role_code = request.POST.get('role')
                if user is None:
                    messages.error(request, 'No account with that username or email. Ask them to register first.')
                elif role_code not in dict(Role.objects.values_list('code', 'name')) or role_code == 'SUPER_ADMIN':
                    messages.error(request, 'Choose a valid role.')
                else:
                    event = organization.events.filter(pk=request.POST.get('event') or 0).first()
                    from billing.service import BillingLimitError, check_limit

                    staff = RoleAssignment.objects.filter(organization=organization).values('user').distinct().count()
                    check_limit(organization, 'max_staff', staff + 1)
                    tenancy.grant_role(organization, user, role_code, request.user, event=event, request=request)
                    messages.success(request, f'{user.username} now has the {role_code.replace("_", " ").title()} role.')
            elif action == 'revoke':
                assignment = get_object_or_404(RoleAssignment, pk=request.POST.get('assignment'), organization=organization)
                tenancy.revoke_role(assignment, request.user, request)
                messages.success(request, 'Role revoked.')
        except (ValueError, Exception) as exc:  # noqa: BLE001 - plan limits / last-admin guard
            if isinstance(exc, PermissionDenied):
                raise
            messages.error(request, str(exc))
        return redirect('console:team_org', org_id=organization.pk)
    assignments = RoleAssignment.objects.filter(organization=organization).select_related('user', 'role', 'event')
    roles = Role.objects.exclude(code__in=['SUPER_ADMIN', 'VOTER'])
    return render(request, 'console/team.html', {'organization': organization, 'assignments': assignments,
                                                 'roles': roles, 'events': organization.events.all(),
                                                 'permissions': PERMISSIONS})


@login_required
def organization_settings(request, org_id):
    organization = get_object_or_404(Organization, pk=org_id)
    if not has_perm(request.user, 'org.manage', organization):
        raise PermissionDenied
    if request.method == 'POST':
        before = audit.snapshot(organization, ['name', 'kind', 'contact_email', 'default_timezone', 'default_currency',
                                               'default_language'])
        organization.name = (request.POST.get('name') or organization.name).strip()[:200]
        if request.POST.get('kind') in Organization.Kind.values:
            organization.kind = request.POST['kind']
        organization.contact_email = (request.POST.get('contact_email') or '').strip()[:254]
        organization.default_timezone = (request.POST.get('default_timezone') or organization.default_timezone)[:64]
        organization.default_currency = (request.POST.get('default_currency') or organization.default_currency)[:3]
        organization.default_language = (request.POST.get('default_language') or 'en')[:8]
        if request.POST.get('sso_issuer'):
            from urllib.parse import urlparse

            issuer = request.POST['sso_issuer'].strip().rstrip('/')
            if urlparse(issuer).scheme != 'https':
                messages.error(request, 'The SSO issuer must be an https URL.')
                return redirect('console:organization', org_id=organization.pk)
            existing = organization.sso_config or {}
            organization.sso_config = {
                'name': (request.POST.get('sso_name') or 'Institutional sign-in')[:80], 'issuer': issuer,
                'client_id': request.POST.get('sso_client_id', '').strip()[:300],
                'client_secret': request.POST.get('sso_client_secret', '').strip()[:500] or existing.get('client_secret', ''),
                'allowed_domains': [d.strip().lower() for d in request.POST.get('sso_domains', '').split(',') if d.strip()],
            }
        elif request.POST.get('sso_clear'):
            organization.sso_config = None
        if request.POST.get('ldap_server_uri'):
            organization.ldap_config = {
                'server_uri': request.POST['ldap_server_uri'].strip()[:300],
                'bind_dn_template': request.POST.get('ldap_bind_dn_template', '').strip()[:300],
                'search_base': request.POST.get('ldap_search_base', '').strip()[:300],
                'search_filter': request.POST.get('ldap_search_filter', '').strip()[:300] or '(uid={username})',
                'identifier_attribute': request.POST.get('ldap_identifier_attribute', '').strip()[:60] or 'uid',
                'start_tls': request.POST.get('ldap_start_tls') == 'on',
            }
        elif request.POST.get('ldap_clear'):
            organization.ldap_config = None
        organization.save()
        changes = audit.diff(before, audit.snapshot(organization, list(before)))
        audit.record('ORGANIZATION_UPDATED', request=request, organization=organization, target=organization,
                     summary='Organization settings updated', changes=changes,
                     metadata={'sso_configured': bool(organization.sso_config), 'ldap_configured': bool(organization.ldap_config)})
        messages.success(request, 'Organization saved.')
        return redirect('console:organization', org_id=organization.pk)
    return render(request, 'console/organization.html', {'organization': organization, 'kinds': Organization.Kind.choices,
                                                         'sso': organization.sso_config or {},
                                                         'ldap': organization.ldap_config or {},
                                                         'sso_callback': f'{settings.SITE_URL}/auth/sso/org-{organization.pk}/callback/'})


@permission_required('platform.admin')
def organizations(request):
    User = get_user_model()
    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()[:200]
        admin_user = User.objects.filter(username=(request.POST.get('admin') or '').strip()).first()
        if not name:
            messages.error(request, 'Name is required.')
        else:
            org = Organization.objects.create(name=name, kind=request.POST.get('kind') or Organization.Kind.OTHER,
                                              contact_email=(request.POST.get('contact_email') or '')[:254])
            audit.record('ORGANIZATION_CREATED', request=request, organization=org, target=org, summary=f'Organization {name} created')
            if admin_user:
                tenancy.grant_role(org, admin_user, 'ORG_ADMIN', request.user, request=request)
            messages.success(request, 'Organization created.')
        return redirect('console:organizations')
    orgs = Organization.objects.annotate(elections=Count('events', distinct=True),
                                         members=Count('role_assignments__user', distinct=True))
    return render(request, 'console/organizations.html', {'organizations': orgs, 'kinds': Organization.Kind.choices})


@permission_required('platform.admin')
def organizers(request):
    from voting.models import Profile

    if request.method == 'POST':
        profile = get_object_or_404(Profile, pk=request.POST.get('profile'))
        approve = request.POST.get('decision') == 'approve'
        profile.is_approved_organizer = approve
        profile.save(update_fields=['is_approved_organizer'])
        if approve:
            tenancy.ensure_personal_organization(profile.user, actor=request.user)
            from notifications.service import notify_user

            notify_user(profile.user, 'organizer_approved', {'username': profile.user.username})
        audit.record('ORGANIZER_APPROVED' if approve else 'ORGANIZER_UNAPPROVED', request=request, target=profile.user,
                     summary=f'Organizer {profile.user.username} {"approved" if approve else "unapproved"}')
        messages.success(request, 'Organizer updated.')
        return redirect('console:organizers')
    return render(request, 'console/organizers.html', {
        'pending': Profile.objects.filter(is_approved_organizer=False).select_related('user').order_by('-user__date_joined'),
        'approved': Profile.objects.filter(is_approved_organizer=True).select_related('user').order_by('user__username')[:200]})


def _requestable_roles():
    return Role.objects.exclude(code__in=NON_REQUESTABLE_ROLES).order_by('name')


def _role_rows(roles):
    """Roles with human-readable permission labels for the request form."""
    return [{'role': role, 'labels': [PERMISSIONS[p] for p in sorted(role.permissions) if p in PERMISSIONS]}
            for role in roles]


@login_required
def access_requests(request):
    """Organizers ask for extra privileges; platform admins grant or refuse them."""
    if is_platform_admin(request.user):
        return _review_access_requests(request)
    if not is_console_user(request.user):
        messages.error(request, 'Your organizer account is pending admin approval.')
        return redirect('home')
    return _my_access_requests(request)


def _resolve_request_scope(request, scope):
    """'org:<id>' or 'event:<id>' -> (organization, event), limited to what the user already works in."""
    kind, _, raw_id = (scope or '').partition(':')
    if not raw_id.isdigit():
        return None, None
    if kind == 'event':
        event = events_for_user(request.user, 'election.view').filter(pk=int(raw_id)).first()
        return (event.organization if event else None), event
    if kind == 'org':
        return organizations_for_user(request.user).filter(pk=int(raw_id)).first(), None
    return None, None


def _my_access_requests(request):
    from notifications.service import notify_platform_admins

    organizations = organizations_for_user(request.user)
    events = events_for_user(request.user, 'election.view').select_related('organization').order_by('-created_at')[:200]
    roles = _requestable_roles()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'cancel':
            req = get_object_or_404(AccessRequest, pk=request.POST.get('request'), requester=request.user,
                                    status=AccessRequest.Status.PENDING)
            req.status = AccessRequest.Status.CANCELLED
            req.save(update_fields=['status'])
            audit.record('ACCESS_REQUEST_CANCELLED', request=request, organization=req.organization, event=req.event,
                         target=req, summary=f'{req.role.name} request cancelled')
            messages.success(request, 'Request cancelled.')
            return redirect('console:access_requests')
        role = roles.filter(code=request.POST.get('role')).first()
        organization, event = _resolve_request_scope(request, request.POST.get('scope'))
        reason = (request.POST.get('reason') or '').strip()[:2000]
        if role is None:
            messages.error(request, 'Choose the role you need.')
        elif organization is None and event is None:
            messages.error(request, 'Choose where you need it: one of your organizations or elections.')
        elif not reason:
            messages.error(request, 'Tell the administrator why you need this access.')
        elif RoleAssignment.objects.filter(user=request.user, role=role, organization=organization, event=event).exists():
            messages.info(request, f'You already have the {role.name} role there.')
        elif AccessRequest.objects.filter(requester=request.user, role=role, organization=organization, event=event,
                                          status=AccessRequest.Status.PENDING).exists():
            messages.info(request, 'You already have a pending request for that role and scope.')
        else:
            try:
                with transaction.atomic():
                    req = AccessRequest.objects.create(requester=request.user, role=role, organization=organization,
                                                       event=event, reason=reason)
            except IntegrityError:
                messages.info(request, 'You already have a pending request for that role and scope.')
                return redirect('console:access_requests')
            audit.record('ACCESS_REQUESTED', request=request, organization=organization, event=event, target=req,
                         summary=f'{role.name} requested ({req.scope_label})', reason=reason,
                         metadata={'role': role.code})
            transaction.on_commit(lambda: notify_platform_admins('access_requested', {
                'username': request.user.username, 'role': role.name, 'scope': req.scope_label, 'reason': reason}))
            messages.success(request, 'Request sent. A platform administrator will review it.')
        return redirect('console:access_requests')
    return render(request, 'console/access_requests.html', {
        'reviewing': False, 'roles': _role_rows(roles), 'organizations': organizations, 'events': events,
        'my_requests': AccessRequest.objects.filter(requester=request.user).select_related(
            'role', 'organization', 'event', 'decided_by')[:100],
        'my_roles': RoleAssignment.objects.filter(user=request.user).select_related('role', 'organization', 'event'),
    })


def _review_access_requests(request):
    from notifications.service import notify_user

    if request.method == 'POST':
        approve = request.POST.get('decision') == 'approve'
        note = (request.POST.get('note') or '').strip()[:1000]
        with transaction.atomic():
            req = get_object_or_404(AccessRequest.objects.select_for_update(), pk=request.POST.get('request'))
            if req.status != AccessRequest.Status.PENDING:
                messages.error(request, 'This request has already been decided.')
                return redirect('console:access_requests')
            requester, role = req.requester, req.role
            if approve:
                organization = req.organization or (req.event.organization if req.event_id else None)
                req.assignment = tenancy.grant_role(organization, requester, role.code, request.user, event=req.event,
                                                    request=request)
            req.status = AccessRequest.Status.APPROVED if approve else AccessRequest.Status.REJECTED
            req.decided_by = request.user
            req.decided_at = timezone.now()
            req.decision_note = note
            req.save()
            audit.record('ACCESS_REQUEST_APPROVED' if approve else 'ACCESS_REQUEST_REJECTED', request=request,
                         organization=req.organization, event=req.event, target=req, reason=note,
                         summary=f'{role.name} for {requester.username} {"granted" if approve else "refused"}',
                         metadata={'role': role.code, 'requester': requester.username})
            context = {'username': requester.username, 'role': role.name, 'scope': req.scope_label,
                       'decision': 'approved' if approve else 'declined', 'note': note}
            transaction.on_commit(lambda: notify_user(requester, 'access_decided', context))
        messages.success(request, f'{role.name} {"granted to" if approve else "refused for"} {requester.username}.')
        return redirect('console:access_requests')
    queryset = AccessRequest.objects.select_related('requester', 'role', 'organization', 'event', 'decided_by')
    pending = list(queryset.filter(status=AccessRequest.Status.PENDING).order_by('created_at'))
    for req in pending:
        req.permission_labels = [PERMISSIONS[p] for p in sorted(req.role.permissions) if p in PERMISSIONS]
    return render(request, 'console/access_requests.html', {
        'reviewing': True, 'pending': pending,
        'history': queryset.exclude(status=AccessRequest.Status.PENDING)[:100],
    })


@permission_required('platform.admin')
def health(request):
    from elections.models import Voter
    from fraud.models import FraudEvent
    from notifications.models import Notification
    from payments.models import Payment, WebhookEvent

    from .backups import backup_status
    from .http import BREAKERS
    from .views import run_health_checks

    now = timezone.now()
    hour = now - timedelta(hours=1)
    attempted = Payment.objects.filter(created_at__gte=hour).count()
    succeeded = Payment.objects.filter(created_at__gte=hour, status='SUCCESS').count()
    context = {
        'checks': run_health_checks(), 'version': settings.APP_VERSION,
        'payments_hour': attempted, 'payment_success_rate': round(100.0 * succeeded / attempted, 1) if attempted else None,
        'webhooks_failed_24h': WebhookEvent.objects.filter(status='FAILED', received_at__gte=now - timedelta(hours=24)).count(),
        'webhooks_24h': WebhookEvent.objects.filter(received_at__gte=now - timedelta(hours=24)).count(),
        'fraud_open': FraudEvent.objects.filter(status='OPEN').count(),
        'notifications_failed': Notification.objects.filter(status='FAILED').count(),
        'notifications_queued': Notification.objects.filter(status='QUEUED').count(),
        'ballots_hour': Voter.objects.filter(voted_at__gte=hour).count(),
        'breakers': {name: breaker.state() for name, breaker in BREAKERS.items()},
        'chains': AuditChainHead.objects.all(),
        'backup': backup_status(),
        'settings_flags': {
            'DEBUG': settings.DEBUG, 'Redis': bool(settings.REDIS_URL), 'Celery broker': not settings.CELERY_TASK_ALWAYS_EAGER,
            'KMS / KEK configured': bool(settings.KMS_KEY_ID or settings.FIELD_ENCRYPTION_KEYS),
            'Signing key configured': bool(settings.SIGNING_PRIVATE_KEY), 'Sentry': bool(settings.SENTRY_DSN),
            'Tracing (OTLP)': bool(settings.OTEL_EXPORTER_OTLP_ENDPOINT), 'Read replica': 'replica' in settings.DATABASES,
            'Paystack': bool(settings.PAYSTACK_SECRET_KEY), 'HSTS seconds': settings.SECURE_HSTS_SECONDS,
            'CAPTCHA': settings.CAPTCHA_PROVIDER or 'honeypot only',
        },
    }
    return render(request, 'console/health.html', context)


@login_required
def support(request):
    if not (is_platform_admin(request.user) or organizations_for_user(request.user, 'support.view').exists()):
        raise PermissionDenied
    tickets = SupportTicket.objects.all() if is_platform_admin(request.user) else SupportTicket.objects.filter(
        organization__in=organizations_for_user(request.user, 'support.view'))
    status = request.GET.get('status')
    if status in SupportTicket.Status.values:
        tickets = tickets.filter(status=status)
    return render(request, 'console/support.html', {'page': Paginator(tickets, 30).get_page(request.GET.get('page')),
                                                    'statuses': SupportTicket.Status.choices, 'status': status})


@login_required
def support_ticket(request, ticket_id):
    ticket = get_object_or_404(SupportTicket, pk=ticket_id)
    allowed = is_platform_admin(request.user) or (ticket.organization_id and has_perm(request.user, 'support.view', ticket.organization))
    if not allowed:
        raise PermissionDenied
    if request.method == 'POST':
        can_manage = is_platform_admin(request.user) or has_perm(request.user, 'support.manage', ticket.organization)
        if not can_manage:
            raise PermissionDenied
        body = (request.POST.get('body') or '').strip()[:10000]
        internal = request.POST.get('internal') == 'on'
        if body:
            SupportMessage.objects.create(ticket=ticket, author=request.user, body=body, is_internal=internal)
            if not internal:
                from notifications.models import Notification
                from notifications.service import notify

                notify('support_ticket', channel=Notification.Channel.EMAIL, recipient=ticket.requester_email,
                       context={'reference': ticket.reference, 'subject': f'Reply: {ticket.subject}'})
        if request.POST.get('status') in SupportTicket.Status.values:
            ticket.status = request.POST['status']
        if request.POST.get('assign_me'):
            ticket.assigned_to = request.user
        ticket.save()
        audit.record('SUPPORT_TICKET_UPDATED', request=request, target=ticket, summary=f'{ticket.reference} updated')
        return redirect('console:support_ticket', ticket_id=ticket.pk)
    return render(request, 'console/support_ticket.html', {'ticket': ticket, 'messages_list': ticket.messages.select_related('author'),
                                                           'statuses': SupportTicket.Status.choices})


@login_required
def notifications_view(request):
    from notifications.models import Notification

    if request.method == 'POST':
        if request.POST.get('action') == 'read_all':
            request.user.notifications.filter(read_at__isnull=True).update(read_at=timezone.now())
        elif request.POST.get('action') == 'retry' and is_platform_admin(request.user):
            from notifications.tasks import deliver

            Notification.objects.filter(pk=request.POST.get('notification'), status='FAILED').update(status='QUEUED')
            deliver.delay(request.POST.get('notification'))
        return redirect('console:notifications')
    outbox = Notification.objects.exclude(channel='IN_APP').select_related('event')[:200] if is_platform_admin(request.user) else []
    return render(request, 'console/notifications.html', {
        'inbox': request.user.notifications.filter(channel='IN_APP')[:100], 'outbox': outbox})


@login_required
def reports(request):
    if not _require_console(request):
        return redirect('home')
    events = events_for_user(request.user, 'election.view').order_by('-created_at')[:200]
    return render(request, 'console/reports.html', {'events': events})
