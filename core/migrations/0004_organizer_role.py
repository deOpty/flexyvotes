"""Approved organizers no longer administer their personal organization.

Until now an approved organizer was made Organization Admin of a personal
organization, which let them approve, open, pause, resume and close their own
elections. They now get the narrower Organizer role (create elections, add
candidates, view results) and request anything else from a platform admin.

Platform staff keep their assignments: they hold every permission anyway.
The permission list is frozen here; ``core.signals.sync_system_roles`` keeps
it in step with ``core.rbac`` after every migrate.
"""
from django.db import migrations

ORGANIZER_PERMISSIONS = sorted([
    'candidate.create', 'candidate.edit', 'election.create', 'election.edit', 'election.submit', 'election.view',
    'results.view', 'vote.view',
])


def forwards(apps, schema_editor):
    Role = apps.get_model('core', 'Role')
    RoleAssignment = apps.get_model('core', 'RoleAssignment')
    organizer, _ = Role.objects.update_or_create(code='ORGANIZER', defaults={
        'name': 'Organizer',
        'description': 'Creates elections, adds candidates and views results. Other privileges are requested from '
                       'a platform admin.',
        'permissions': ORGANIZER_PERMISSIONS, 'is_system': True,
    })
    personal_admins = RoleAssignment.objects.filter(
        role__code='ORG_ADMIN', organization__is_personal=True, event__isnull=True,
        user__is_staff=False, user__is_superuser=False)
    for assignment in personal_admins:
        duplicate = RoleAssignment.objects.filter(user_id=assignment.user_id, role=organizer,
                                                  organization_id=assignment.organization_id, event__isnull=True)
        if duplicate.exists():
            assignment.delete()
        else:
            assignment.role = organizer
            assignment.save(update_fields=['role'])


def backwards(apps, schema_editor):
    Role = apps.get_model('core', 'Role')
    RoleAssignment = apps.get_model('core', 'RoleAssignment')
    org_admin = Role.objects.filter(code='ORG_ADMIN').first()
    if org_admin is None:
        return
    RoleAssignment.objects.filter(role__code='ORGANIZER', organization__is_personal=True,
                                  event__isnull=True).update(role=org_admin)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_accessrequest'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
