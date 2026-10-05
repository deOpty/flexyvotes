"""Carry existing data into the platform models.

* Every organizer gets a personal Organization (ORG_ADMIN) and their events
  are scoped to it (multi-tenancy).
* Event.is_approved / voting_locked / dates -> explicit lifecycle status.
* ActivityLog rows -> hash-chained core.AuditEvent rows.
* VotingCode rows -> elections.Voter roll entries. Credentials keep the same
  HMAC scheme, so codes already handed out keep working.
"""
from django.db import migrations
from django.utils import timezone
from django.utils.text import slugify


def forwards(apps, schema_editor):
    from core.audit import GENESIS, chain_for, compute_hash
    from core.crypto import blind_index
    from core.rbac import ROLE_DEFINITIONS

    Event = apps.get_model('voting', 'Event')
    Profile = apps.get_model('voting', 'Profile')
    ActivityLog = apps.get_model('voting', 'ActivityLog')
    VotingCode = apps.get_model('voting', 'VotingCode')
    Organization = apps.get_model('core', 'Organization')
    Role = apps.get_model('core', 'Role')
    RoleAssignment = apps.get_model('core', 'RoleAssignment')
    AuditEvent = apps.get_model('core', 'AuditEvent')
    AuditChainHead = apps.get_model('core', 'AuditChainHead')
    Voter = apps.get_model('elections', 'Voter')
    User = apps.get_model('auth', 'User')

    for code, (name, description, permissions) in ROLE_DEFINITIONS.items():
        Role.objects.update_or_create(code=code, defaults={
            'name': name, 'description': description, 'permissions': sorted(permissions), 'is_system': True,
        })
    org_admin = Role.objects.get(code='ORG_ADMIN')

    orgs = {}

    def org_for(user):
        if user.pk not in orgs:
            base = slugify(user.username)[:50] or 'org'
            slug, n = base, 1
            while Organization.objects.filter(slug=slug).exists():
                n += 1
                slug = f'{base}-{n}'
            org = Organization.objects.create(name=f"{user.username}'s organization", slug=slug,
                                              is_personal=True, contact_email=user.email or '')
            RoleAssignment.objects.get_or_create(user=user, role=org_admin, organization=org, event=None)
            orgs[user.pk] = org
        return orgs[user.pk]

    for profile in Profile.objects.filter(is_approved_organizer=True):
        org_for(User.objects.get(pk=profile.user_id))

    now = timezone.now()
    for event in Event.objects.all():
        if event.organizer_id:
            event.organization = org_for(User.objects.get(pk=event.organizer_id))
        if not event.is_approved:
            event.status = 'REVIEW'
        elif now < event.start_date:
            event.status = 'SCHEDULED'
        elif now < event.end_date and not event.voting_locked:
            event.status = 'OPEN'
            event.opened_at = event.start_date
        elif now < event.end_date and event.voting_locked:
            event.status = 'PAUSED'
            event.opened_at = event.start_date
        else:
            event.status = 'CLOSED'
            event.opened_at = event.start_date
            event.closed_at = event.end_date
        if event.voting_mode == 'Code Voting':
            event.results_visibility = 'AFTER_PUBLISH'
            event.auth_methods = ['CODE']
        event.save()

    heads = {}
    for log in ActivityLog.objects.order_by('created_at', 'id'):
        organization_id = None
        if log.event_id:
            organization_id = Event.objects.filter(pk=log.event_id).values_list('organization_id', flat=True).first()
        chain = chain_for(organization_id)
        seq, prev = heads.get(chain, (0, GENESIS))
        user = User.objects.filter(pk=log.user_id).first() if log.user_id else None
        fields = {
            'chain': chain, 'seq': seq + 1, 'event_type': 'LEGACY_ACTIVITY',
            'actor_id': log.user_id, 'actor_label': user.username if user else '',
            'organization_id': organization_id, 'election_id': log.event_id,
            'target_type': 'voting.Event' if log.event_id else '', 'target_id': str(log.event_id or ''),
            'summary': log.action[:500], 'ip_address': None, 'user_agent': '', 'correlation_id': '',
            'changes': {}, 'metadata': {'migrated_from': 'voting.ActivityLog', 'legacy_id': log.pk},
            'result': 'SUCCESS', 'reason': '', 'created_at': log.created_at,
        }
        fields['prev_hash'] = prev
        fields['hash'] = compute_hash(prev, fields)
        AuditEvent.objects.create(**fields)
        heads[chain] = (seq + 1, fields['hash'])
    for chain, (seq, last_hash) in heads.items():
        AuditChainHead.objects.update_or_create(chain=chain, defaults={'seq': seq, 'last_hash': last_hash})

    seen = set()
    for code in VotingCode.objects.order_by('id'):
        if code.invalidated_at:
            continue  # superseded by a reset; the replacement row is migrated instead
        identifier = (code.voter_identifier or '').strip().upper() or None
        if identifier:
            key = (code.event_id, identifier)
            if key in seen:
                continue
            seen.add(key)
        email = (code.voter_email or '').strip() or None
        Voter.objects.create(
            election_id=code.event_id,
            identifier=identifier,
            email=email,
            email_index=blind_index(email, 'email') if email else '',
            credential_hash=code.code_hash or None,
            credential_ciphertext=(code.code or None) if not code.is_used else None,
            credential_issued_at=code.created_at,
            credential_version=1,
            status='VOTED' if code.is_used else 'ELIGIBLE',
            voted_at=code.used_at if code.is_used else None,
            source='LEGACY' if identifier else 'CODES',
        )


class Migration(migrations.Migration):

    dependencies = [
        ('voting', '0036_platform_election_fields'),
        ('core', '0001_initial'),
        ('elections', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
