"""Tenant (organization) resolution helpers."""
from django.db import transaction

from . import audit
from .models import Organization, Role, RoleAssignment
from .rbac import clear_cache, is_platform_admin, organizations_for_user

SESSION_KEY = 'fv_org'


def ensure_personal_organization(user, actor=None):
    """Approved organizers administer their own organization."""
    existing = RoleAssignment.objects.filter(user=user, role__code='ORG_ADMIN', organization__isnull=False,
                                             event__isnull=True).select_related('organization').first()
    if existing:
        return existing.organization
    with transaction.atomic():
        org = Organization.objects.create(name=f"{user.get_full_name() or user.username}'s organization",
                                          is_personal=True, contact_email=user.email or '')
        role = Role.objects.get(code='ORG_ADMIN')
        RoleAssignment.objects.create(user=user, role=role, organization=org, granted_by=actor)
        audit.record('ORGANIZATION_CREATED', actor=actor, organization=org, target=org,
                     summary=f'Personal organization created for {user.username}')
    clear_cache(user)
    return org


def current_organization(request, perm='election.create'):
    user = request.user
    if not user.is_authenticated:
        return None
    candidates = organizations_for_user(user, perm)
    selected = request.session.get(SESSION_KEY)
    if selected:
        org = candidates.filter(pk=selected).first()
        if org:
            return org
    org = candidates.order_by('-is_personal', 'name').first()
    if org is None and is_platform_admin(user):
        org = ensure_personal_organization(user, actor=user)
    return org


def switch_organization(request, organization_id):
    org = organizations_for_user(request.user).filter(pk=organization_id).first()
    if org is not None:
        request.session[SESSION_KEY] = org.pk
    return org


def grant_role(organization, user, role_code, actor, event=None, request=None):
    role = Role.objects.get(code=role_code)
    assignment, created = RoleAssignment.objects.get_or_create(user=user, role=role, organization=organization,
                                                               event=event, defaults={'granted_by': actor})
    if created:
        audit.record('ROLE_GRANTED', request=request, actor=actor, organization=organization, event=event,
                     target=user, summary=f'{role.name} granted to {user.username}',
                     metadata={'role': role_code, 'scope': f'election:{event.pk}' if event else 'organization'})
    clear_cache(user)
    return assignment


def revoke_role(assignment, actor, request=None):
    user, role, organization, event = assignment.user, assignment.role, assignment.organization, assignment.event
    if role.code == 'ORG_ADMIN' and organization is not None and RoleAssignment.objects.filter(
            organization=organization, role__code='ORG_ADMIN', event__isnull=True).count() <= 1:
        raise ValueError('An organization must keep at least one Organization Admin.')
    assignment.delete()
    audit.record('ROLE_REVOKED', request=request, actor=actor, organization=organization, event=event, target=user,
                 summary=f'{role.name} revoked from {user.username}', metadata={'role': role.code})
    clear_cache(user)
