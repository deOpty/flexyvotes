"""Permission-based authorization.

Code never checks role names: it asks ``has_perm(user, 'election.publish',
event)``. Roles are just named bundles of permissions (seeded as system
roles, synced on every migrate), granted platform-wide, per organization, or
per election through ``RoleAssignment``.
"""
from functools import wraps

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404

PERMISSIONS = {
    'election.create': 'Create elections',
    'election.view': 'View election configuration',
    'election.edit': 'Edit election configuration, positions and rules',
    'election.submit': 'Submit an election for review',
    'election.review': 'Approve or reject an election configuration',
    'election.publish': 'Schedule / open an approved election',
    'election.pause': 'Pause and resume voting',
    'election.close': 'Close voting',
    'election.archive': 'Archive elections',
    'election.freeze': 'Freeze or request unfreeze of configuration',
    'candidate.create': 'Add candidates',
    'candidate.edit': 'Edit and withdraw candidates',
    'voter.view': 'View the voter roll',
    'voter.import': 'Import voters',
    'voter.edit': 'Edit, verify and suspend voters',
    'voter.credentials': 'Issue, export and reset voter credentials',
    'vote.view': 'View turnout and aggregate vote activity',
    'vote.export': 'Export vote ledgers',
    'results.view': 'View results before publication',
    'results.tally': 'Run the tally',
    'results.approve': 'Approve and certify tallied results',
    'results.certify': 'Certify results',
    'results.publish': 'Publish certified results',
    'results.recount': 'Run recounts',
    'payment.view': 'View payments and revenue',
    'payment.reconcile': 'Run payment reconciliation',
    'refund.create': 'Request refunds',
    'refund.approve': 'Approve refunds',
    'pricing.manage': 'Manage vote packages and discount codes',
    'audit.view': 'View audit logs',
    'fraud.view': 'View fraud alerts',
    'fraud.review': 'Review held transactions and fraud cases',
    'dispute.view': 'View disputes',
    'dispute.manage': 'Manage disputes and evidence',
    'incident.manage': 'Manage incidents',
    'approval.decide': 'Approve or reject dual-approval requests',
    'org.manage': 'Manage organization members and roles',
    'org.billing': 'Manage subscription and invoices',
    'support.view': 'View support tickets',
    'support.manage': 'Respond to support tickets',
    'ticket.manage': 'Manage event tickets and check-in',
    'store.manage': 'Manage the merchandise store',
    'platform.admin': 'Platform administration',
}

ALL_PERMISSIONS = frozenset(PERMISSIONS)

_ORG_ADMIN = ALL_PERMISSIONS - {'platform.admin', 'store.manage'}

ROLE_DEFINITIONS = {
    'SUPER_ADMIN': ('Super Admin', 'Full platform access.', ALL_PERMISSIONS),
    'ORG_ADMIN': ('Organization Admin', 'Administers an organization and all of its elections.', _ORG_ADMIN),
    'ELECTION_ADMIN': ('Election Administrator', 'Creates and configures elections.', {
        'election.create', 'election.view', 'election.edit', 'election.submit', 'election.publish',
        'election.pause', 'election.close', 'election.archive', 'election.freeze',
        'candidate.create', 'candidate.edit', 'voter.view', 'voter.import', 'voter.edit', 'voter.credentials',
        'vote.view', 'results.view', 'results.tally', 'pricing.manage', 'ticket.manage', 'audit.view',
        'dispute.view', 'incident.manage', 'payment.view',
    }),
    'ELECTION_REVIEWER': ('Election Reviewer', 'Independently approves election configurations and sensitive actions.', {
        'election.view', 'election.review', 'approval.decide', 'audit.view', 'voter.view', 'vote.view',
    }),
    'ELECTION_OFFICER': ('Election Officer', 'Runs polling operations: voter assistance and credentials.', {
        'election.view', 'election.pause', 'voter.view', 'voter.edit', 'voter.credentials', 'vote.view',
        'incident.manage', 'dispute.view', 'support.view',
    }),
    'ELECTION_AUDITOR': ('Election Auditor', 'Read-only independent oversight, including recounts.', {
        'election.view', 'voter.view', 'vote.view', 'results.view', 'results.recount', 'audit.view',
        'payment.view', 'fraud.view', 'dispute.view',
    }),
    'CANDIDATE_MANAGER': ('Candidate Manager', 'Maintains candidate profiles.', {
        'election.view', 'candidate.create', 'candidate.edit',
    }),
    'FINANCE_OFFICER': ('Finance Officer', 'Payments, refunds, reconciliation and billing.', {
        'election.view', 'payment.view', 'payment.reconcile', 'refund.create', 'refund.approve',
        'pricing.manage', 'org.billing', 'vote.view',
    }),
    'SUPPORT_AGENT': ('Support Agent', 'Handles voter and organizer support requests.', {
        'election.view', 'voter.view', 'payment.view', 'support.view', 'support.manage',
    }),
    'FRAUD_ANALYST': ('Fraud Analyst', 'Investigates risk alerts and held transactions.', {
        'election.view', 'fraud.view', 'fraud.review', 'payment.view', 'vote.view', 'audit.view',
    }),
    'RESULTS_OFFICER': ('Results Officer', 'Tallies, approves, certifies and publishes results.', {
        'election.view', 'vote.view', 'results.view', 'results.tally', 'results.approve', 'results.certify',
        'results.publish', 'results.recount',
    }),
    'VOTER': ('Voter', 'Marker role for voter accounts; grants no administrative permissions.', set()),
}

# What the creator/organizer of an event can always do on it (legacy
# organizer accounts relied on Event.organizer for access).
ORGANIZER_PERMISSIONS = ROLE_DEFINITIONS['ELECTION_ADMIN'][2] | {'election.submit'}


def _assignments(user):
    cached = getattr(user, '_fv_role_assignments', None)
    if cached is None:
        from .models import RoleAssignment

        cached = [
            (frozenset(a.role.permissions), a.organization_id, a.event_id)
            for a in RoleAssignment.objects.filter(user=user).select_related('role')
        ]
        user._fv_role_assignments = cached
    return cached


def clear_cache(user):
    if hasattr(user, '_fv_role_assignments'):
        del user._fv_role_assignments


def is_platform_admin(user):
    return bool(user and user.is_authenticated and user.is_active and (user.is_superuser or user.is_staff))


def _resolve_scope(obj):
    """Return (organization_id, event) for an object we authorize against."""
    from core.models import Organization
    from voting.models import Event

    if obj is None:
        return None, None
    if isinstance(obj, Event):
        return obj.organization_id, obj
    if isinstance(obj, Organization):
        return obj.pk, None
    event = getattr(obj, 'event', None) or getattr(obj, 'election', None)
    if isinstance(event, Event):
        return event.organization_id, event
    organization_id = getattr(obj, 'organization_id', None)
    return organization_id, None


def user_permissions(user, obj=None):
    if not user or not user.is_authenticated or not user.is_active:
        return frozenset()
    if is_platform_admin(user):
        return ALL_PERMISSIONS
    organization_id, event = _resolve_scope(obj)
    granted = set()
    for perms, org_id, event_id in _assignments(user):
        if org_id is None and event_id is None:
            granted |= perms
        elif event_id is not None:
            if event is not None and event.pk == event_id:
                granted |= perms
        elif organization_id is not None and org_id == organization_id:
            granted |= perms
    if event is not None and event.organizer_id == user.pk:
        granted |= ORGANIZER_PERMISSIONS
    return frozenset(granted)


def has_perm(user, perm, obj=None):
    if perm not in PERMISSIONS:
        raise ValueError(f'Unknown permission {perm!r}')
    return perm in user_permissions(user, obj)


def has_any_perm(user, perms, obj=None):
    granted = user_permissions(user, obj)
    return any(p in granted for p in perms)


def check_perm(user, perm, obj=None):
    if not has_perm(user, perm, obj):
        raise PermissionDenied(f'Missing permission: {perm}')


def organizations_for_user(user, perm=None):
    from .models import Organization

    if is_platform_admin(user):
        return Organization.objects.all()
    if not user.is_authenticated:
        return Organization.objects.none()
    ids = {org_id for perms, org_id, event_id in _assignments(user)
           if org_id is not None and event_id is None and (perm is None or perm in perms)}
    return Organization.objects.filter(pk__in=ids)


def events_for_user(user, perm='election.view'):
    """All elections the user may exercise ``perm`` on (tenant scoping)."""
    from voting.models import Event

    if is_platform_admin(user):
        return Event.objects.all()
    if not user.is_authenticated:
        return Event.objects.none()
    org_ids, event_ids, platform = set(), set(), False
    for perms, org_id, event_id in _assignments(user):
        if perm not in perms:
            continue
        if org_id is None and event_id is None:
            platform = True
        elif event_id is not None:
            event_ids.add(event_id)
        else:
            org_ids.add(org_id)
    if platform:
        return Event.objects.all()
    query = Q(organization_id__in=org_ids) | Q(pk__in=event_ids)
    if perm in ORGANIZER_PERMISSIONS:
        query |= Q(organizer=user)
    return Event.objects.filter(query)


def is_console_user(user):
    """Has any administrative access at all (used for navigation)."""
    if is_platform_admin(user):
        return True
    if not user.is_authenticated:
        return False
    return bool(_assignments(user)) or user.events.exists()


def event_permission_required(perm, lookup='event_id'):
    """View decorator: load voting.Event from the URL kwarg and require perm.

    The loaded event is passed to the view as ``event`` (replacing the id).
    Missing permission returns 403 and is written to the audit log.
    """

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            from voting.models import Event

            from . import audit

            if not request.user.is_authenticated:
                from django.contrib.auth.views import redirect_to_login

                return redirect_to_login(request.get_full_path())
            event = get_object_or_404(Event, pk=kwargs.pop(lookup))
            if not has_perm(request.user, perm, event):
                audit.record('ACCESS_DENIED', request=request, event=event, result='DENIED',
                             summary=f'Missing {perm} on election {event.pk}', metadata={'permission': perm})
                raise PermissionDenied
            return view(request, *args, event=event, **kwargs)

        return wrapper

    return decorator


def permission_required(perm):
    """View decorator for platform-level (non-election) permissions."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                from django.contrib.auth.views import redirect_to_login

                return redirect_to_login(request.get_full_path())
            if not (is_platform_admin(request.user) or any(perm in perms for perms, _, _ in _assignments(request.user))):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
