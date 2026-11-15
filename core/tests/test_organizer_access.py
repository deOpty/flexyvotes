"""Organizers build elections and follow results; everything else is an
administrator's job, granted on request (AccessRequest)."""
import importlib
import json
from datetime import timedelta

from django.apps import apps
from django.test import TestCase
from django.urls import reverse

from core import tenancy
from core.auth import create_api_token
from core.models import AccessRequest, AuditEvent, Organization, Role, RoleAssignment
from core.rbac import has_perm, permissions_anywhere
from core.tests.factories import PASSWORD, add_position, ensure_roles, make_event, make_user
from notifications.models import Notification
from voting.models import Candidate, Category, Event, Profile

ADMIN_ONLY = ('election.review', 'election.publish', 'election.pause', 'election.close', 'election.settings',
              'election.reschedule', 'election.archive', 'election.freeze', 'voter.view', 'voter.import',
              'voter.credentials', 'payment.view', 'audit.view', 'fraud.view', 'org.manage', 'org.billing',
              'approval.decide', 'results.tally', 'results.certify', 'results.publish', 'ticket.manage',
              'pricing.manage', 'platform.admin')


def approved_organizer(username='organizer', admin=None):
    """An organizer as created by the real approval path."""
    ensure_roles()
    user = make_user(username)
    Profile.objects.create(user=user, is_approved_organizer=True)
    org = tenancy.ensure_personal_organization(user, actor=admin)
    return user, org


class OrganizerRoleTests(TestCase):
    def setUp(self):
        self.admin = make_user('platform', staff=True, superuser=True)
        self.organizer, self.org = approved_organizer(admin=self.admin)
        self.event = make_event(org=self.org, organizer=self.organizer, institutional=False,
                                start_offset=timedelta(days=1), end_offset=timedelta(days=2))

    def test_approval_grants_organizer_role_not_org_admin(self):
        codes = set(RoleAssignment.objects.filter(user=self.organizer).values_list('role__code', flat=True))
        self.assertEqual(codes, {'ORGANIZER'})
        self.assertTrue(self.org.is_personal)
        # Approving again is idempotent.
        self.assertEqual(tenancy.ensure_personal_organization(self.organizer), self.org)
        self.assertEqual(RoleAssignment.objects.filter(user=self.organizer).count(), 1)

    def test_admin_approval_view_grants_organizer_role(self):
        pending = make_user('newcomer')
        profile = Profile.objects.create(user=pending, is_approved_organizer=False)
        self.client.login(username='platform', password=PASSWORD)
        self.client.post(reverse('console:organizers'), {'profile': profile.pk, 'decision': 'approve'})
        self.assertEqual(list(RoleAssignment.objects.filter(user=pending).values_list('role__code', flat=True)),
                         ['ORGANIZER'])

    def test_organizer_permissions_are_create_candidates_and_results_only(self):
        for perm in ('election.create', 'election.view', 'election.edit', 'election.submit', 'candidate.create',
                     'candidate.edit', 'results.view', 'vote.view'):
            self.assertTrue(has_perm(self.organizer, perm, self.event), perm)
        for perm in ADMIN_ONLY:
            self.assertFalse(has_perm(self.organizer, perm, self.event), perm)
            self.assertNotIn(perm, permissions_anywhere(self.organizer))

    def test_event_creator_without_any_role_gets_only_organizer_permissions(self):
        legacy = make_user('legacy')
        event = make_event(organizer=legacy, institutional=False)
        self.assertTrue(has_perm(legacy, 'candidate.create', event))
        self.assertFalse(has_perm(legacy, 'election.publish', event))
        self.assertFalse(has_perm(legacy, 'election.pause', event))

    def test_organizer_cannot_approve_start_pause_resume_or_close(self):
        self.client.login(username='organizer', password=PASSWORD)
        cases = [(Event.Status.REVIEW, 'approve'), (Event.Status.APPROVED, 'schedule'),
                 (Event.Status.SCHEDULED, 'open'), (Event.Status.OPEN, 'pause'), (Event.Status.PAUSED, 'resume'),
                 (Event.Status.OPEN, 'close')]
        # Start stays in the future so the automatic scheduler never opens it.
        for status, action in cases:
            Event.objects.filter(pk=self.event.pk).update(status=status)
            response = self.client.post(reverse('elections:console_transition', args=[self.event.pk]),
                                        {'action': action}, follow=True)
            self.assertContains(response, 'You do not have permission to do that.')
            self.event.refresh_from_db()
            self.assertEqual(self.event.status, status, action)
        # Legacy pause toggle and dashboard approval are refused too.
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.OPEN)
        self.assertEqual(self.client.post(reverse('toggle_voting_lock', args=[self.event.pk])).status_code, 403)
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.REVIEW)
        self.client.post(reverse('approve_event', args=[self.event.pk]))
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.REVIEW)

    def test_organizer_cannot_transition_through_the_api(self):
        raw, _ = create_api_token(self.organizer, 'tests')
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.REVIEW)
        response = self.client.post(f'/api/v1/elections/{self.event.pk}/transitions', json.dumps({'action': 'approve'}),
                                    content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {raw}')
        self.assertEqual(response.status_code, 403)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.REVIEW)

    def test_organizer_can_build_submit_and_see_results(self):
        self.client.login(username='organizer', password=PASSWORD)
        self.client.post(reverse('add_category', args=[self.event.pk]), {'name': 'Best Artist'})
        category = Category.objects.get(event=self.event, name='Best Artist')
        self.client.post(reverse('add_candidate', args=[self.event.pk]), {'name': 'Ama', 'category': category.pk})
        self.assertTrue(Candidate.objects.filter(event=self.event, name='Ama').exists())
        self.client.post(reverse('elections:console_transition', args=[self.event.pk]), {'action': 'submit'})
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.REVIEW)
        for name in ('elections:console_overview', 'elections:console_ballot', 'elections:console_results',
                     'elections:console_monitor', 'event_analytics'):
            self.assertEqual(self.client.get(reverse(name, args=[self.event.pk])).status_code, 200, name)
        results = self.client.get(reverse('elections:console_results', args=[self.event.pk]))
        self.assertTrue(results.context['can_view_results'])

    def test_admin_pages_are_forbidden_to_organizers(self):
        institutional = make_event(org=self.org, organizer=self.organizer)
        self.client.login(username='organizer', password=PASSWORD)
        for name, args in [('elections:console_settings', [self.event.pk]), ('elections:console_integrity', [self.event.pk]),
                           ('elections:console_audit', [self.event.pk]), ('elections:console_schedule', [self.event.pk]),
                           ('elections:console_voters', [institutional.pk]),
                           ('elections:console_eligibility', [institutional.pk]),
                           ('payments:pricing', [self.event.pk]), ('event_guestlist', [self.event.pk]),
                           ('console:audit', []), ('console:team', []), ('console:approvals', []),
                           ('console:organizations', []), ('console:organizers', []), ('console:health', []),
                           ('console:support', []), ('billing:home', []), ('payments:console_list', []),
                           ('payments:revenue', []), ('payments:refunds', []), ('payments:reconciliation', []),
                           ('fraud:alerts', [])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403, name)

    def test_monitor_feed_hides_payments_and_fraud_from_organizers(self):
        self.client.login(username='organizer', password=PASSWORD)
        data = self.client.get(reverse('elections:console_monitor_data', args=[self.event.pk])).json()
        for key in ('open_alerts', 'latest_alerts', 'payments_last_hour', 'held'):
            self.assertNotIn(key, data)
        self.client.login(username='platform', password=PASSWORD)
        data = self.client.get(reverse('elections:console_monitor_data', args=[self.event.pk])).json()
        self.assertIn('open_alerts', data)
        self.assertIn('payments_last_hour', data)


class OrganizerMenuTests(TestCase):
    """The organizer only sees menus and items they can use."""

    HIDDEN_FROM_ORGANIZER = ['payments:console_list', 'payments:revenue', 'payments:reconciliation', 'payments:refunds',
                             'fraud:alerts', 'console:audit', 'console:team', 'billing:home', 'console:approvals',
                             'console:support', 'console:organizations', 'console:organizers', 'console:health']

    def setUp(self):
        self.admin = make_user('platform', staff=True, superuser=True)
        self.organizer, self.org = approved_organizer(admin=self.admin)
        self.event = make_event(org=self.org, organizer=self.organizer)

    def test_organizer_sidebar_and_dashboard(self):
        self.client.login(username='organizer', password=PASSWORD)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for name in self.HIDDEN_FROM_ORGANIZER:
            self.assertNotIn(f'href="{reverse(name)}"', html, name)
        for name in ('elections:console_list', 'create_event', 'console:reports', 'console:notifications',
                     'console:access_requests'):
            self.assertIn(f'href="{reverse(name)}"', html, name)
        self.assertNotContains(response, 'Revenue (30 days)')
        self.assertNotContains(response, 'Fraud alerts')
        self.assertNotContains(response, 'Recent activity')
        self.assertContains(response, 'Need more access?')

    def test_organizer_election_tabs(self):
        self.client.login(username='organizer', password=PASSWORD)
        html = self.client.get(reverse('elections:console_overview', args=[self.event.pk])).content.decode()
        for name in ('elections:console_settings', 'elections:console_voters', 'elections:console_eligibility',
                     'elections:console_integrity', 'elections:console_audit', 'elections:console_schedule',
                     'create_ticket'):
            self.assertNotIn(f'href="{reverse(name, args=[self.event.pk])}"', html, name)
        for name in ('elections:console_ballot', 'elections:console_results', 'elections:console_monitor', 'edit_event'):
            self.assertIn(f'href="{reverse(name, args=[self.event.pk])}"', html, name)
        # Only "Submit for review" is offered as a lifecycle action.
        self.assertIn('value="submit"', html)
        for action in ('approve', 'schedule', 'open', 'pause', 'close'):
            self.assertNotIn(f'value="{action}"', html)

    def test_reports_page_shows_results_only(self):
        self.client.login(username='organizer', password=PASSWORD)
        response = self.client.get(reverse('console:reports'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('elections:console_results_export', args=[self.event.pk, 'csv']))
        self.assertNotContains(response, reverse('payments:revenue'))
        self.assertNotContains(response, reverse('elections:console_voters_export', args=[self.event.pk]))

    def test_admin_sees_everything(self):
        self.client.login(username='platform', password=PASSWORD)
        html = self.client.get(reverse('dashboard')).content.decode()
        for name in self.HIDDEN_FROM_ORGANIZER + ['console:access_requests']:
            self.assertIn(f'href="{reverse(name)}"', html, name)
        html = self.client.get(reverse('elections:console_overview', args=[self.event.pk])).content.decode()
        for name in ('elections:console_settings', 'elections:console_voters', 'elections:console_integrity',
                     'elections:console_audit', 'elections:console_schedule'):
            self.assertIn(f'href="{reverse(name, args=[self.event.pk])}"', html, name)


class AccessRequestTests(TestCase):
    def setUp(self):
        self.admin = make_user('platform', staff=True, superuser=True)
        self.organizer, self.org = approved_organizer(admin=self.admin)
        self.event = make_event(org=self.org, organizer=self.organizer, institutional=False,
                                status=Event.Status.OPEN)
        self.url = reverse('console:access_requests')

    def request_role(self, role='ELECTION_OFFICER', scope=None, reason='I run polling day'):
        return self.client.post(self.url, {'action': 'request', 'role': role,
                                           'scope': scope or f'event:{self.event.pk}', 'reason': reason})

    def test_full_request_and_grant_flow(self):
        self.client.login(username='organizer', password=PASSWORD)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        with self.captureOnCommitCallbacks(execute=True):
            self.request_role()
        req = AccessRequest.objects.get()
        self.assertEqual((req.status, req.role.code, req.event_id), ('PENDING', 'ELECTION_OFFICER', self.event.pk))
        self.assertTrue(AuditEvent.objects.filter(event_type='ACCESS_REQUESTED').exists())
        self.assertTrue(Notification.objects.filter(user=self.admin, template='access_requested').exists())
        # A duplicate pending request is refused.
        self.request_role()
        self.assertEqual(AccessRequest.objects.count(), 1)
        self.assertFalse(has_perm(self.organizer, 'election.pause', self.event))

        self.client.login(username='platform', password=PASSWORD)
        self.assertContains(self.client.get(self.url), 'I run polling day')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, {'request': req.pk, 'decision': 'approve', 'note': 'OK for this election'})
        req.refresh_from_db()
        self.assertEqual(req.status, AccessRequest.Status.APPROVED)
        self.assertEqual(req.decided_by, self.admin)
        self.assertIsNotNone(req.assignment)
        self.assertTrue(Notification.objects.filter(user=self.organizer, template='access_decided').exists())

        # The organizer can now pause their election, and only that one.
        organizer = type(self.organizer).objects.get(pk=self.organizer.pk)
        self.assertTrue(has_perm(organizer, 'election.pause', self.event))
        other = make_event(org=self.org, organizer=organizer, institutional=False)
        self.assertFalse(has_perm(organizer, 'election.pause', other))
        self.client.login(username='organizer', password=PASSWORD)
        self.client.post(reverse('elections:console_transition', args=[self.event.pk]), {'action': 'pause'})
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.PAUSED)
        # Deciding twice is refused.
        self.client.login(username='platform', password=PASSWORD)
        self.client.post(self.url, {'request': req.pk, 'decision': 'reject'})
        req.refresh_from_db()
        self.assertEqual(req.status, AccessRequest.Status.APPROVED)

    def test_reject_and_cancel(self):
        self.client.login(username='organizer', password=PASSWORD)
        self.request_role('FINANCE_OFFICER', scope=f'org:{self.org.pk}', reason='See revenue')
        self.request_role('ELECTION_ADMIN', reason='Publish myself')
        finance, admin_req = AccessRequest.objects.order_by('pk')
        self.client.post(self.url, {'action': 'cancel', 'request': finance.pk})
        finance.refresh_from_db()
        self.assertEqual(finance.status, AccessRequest.Status.CANCELLED)
        self.client.login(username='platform', password=PASSWORD)
        self.client.post(self.url, {'request': admin_req.pk, 'decision': 'reject', 'note': 'Not now'})
        admin_req.refresh_from_db()
        self.assertEqual((admin_req.status, admin_req.decision_note), (AccessRequest.Status.REJECTED, 'Not now'))
        self.assertFalse(RoleAssignment.objects.filter(user=self.organizer, role__code='ELECTION_ADMIN').exists())

    def test_invalid_requests_are_refused(self):
        foreign = make_event(org=Organization.objects.create(name='Other'), institutional=False)
        self.client.login(username='organizer', password=PASSWORD)
        self.request_role('SUPER_ADMIN')
        self.request_role('ORGANIZER')
        self.request_role(scope=f'event:{foreign.pk}')
        self.request_role(scope='org:999999')
        self.request_role(reason='')
        self.assertFalse(AccessRequest.objects.exists())

    def test_organizer_cannot_decide_requests(self):
        req = AccessRequest.objects.create(requester=self.organizer, role=Role.objects.get(code='ELECTION_ADMIN'),
                                           organization=self.org, reason='x')
        self.client.login(username='organizer', password=PASSWORD)
        self.client.post(self.url, {'request': req.pk, 'decision': 'approve'})
        req.refresh_from_db()
        self.assertEqual(req.status, AccessRequest.Status.PENDING)
        self.assertFalse(RoleAssignment.objects.filter(user=self.organizer, role__code='ELECTION_ADMIN').exists())

    def test_unapproved_user_is_sent_home(self):
        make_user('stranger')
        self.client.login(username='stranger', password=PASSWORD)
        self.assertRedirects(self.client.get(self.url), reverse('home'), fetch_redirect_response=False)


class AdminApprovalPublishesTests(TestCase):
    def test_dashboard_approval_also_publishes(self):
        admin = make_user('platform', staff=True, superuser=True)
        organizer, org = approved_organizer(admin=admin)
        event = make_event(org=org, organizer=organizer, institutional=False, status=Event.Status.REVIEW,
                           start_offset=timedelta(days=1), end_offset=timedelta(days=2))
        add_position(event, 'Best Artist', candidates=('A', 'B'))
        self.client.login(username='platform', password=PASSWORD)
        self.client.post(reverse('approve_event', args=[event.pk]))
        event.refresh_from_db()
        self.assertEqual(event.status, Event.Status.SCHEDULED)
        self.assertTrue(event.is_public)


class OrganizerRoleMigrationTests(TestCase):
    def test_personal_org_admins_become_organizers(self):
        ensure_roles()
        migration = importlib.import_module('core.migrations.0004_organizer_role')
        org_admin = Role.objects.get(code='ORG_ADMIN')
        organizer = make_user('legacy_org')
        staff = make_user('staffer', staff=True)
        institution_admin = make_user('uni_admin')
        personal = Organization.objects.create(name='Personal', is_personal=True)
        staff_personal = Organization.objects.create(name='Staff personal', is_personal=True)
        university = Organization.objects.create(name='University')
        RoleAssignment.objects.create(user=organizer, role=org_admin, organization=personal)
        RoleAssignment.objects.create(user=staff, role=org_admin, organization=staff_personal)
        RoleAssignment.objects.create(user=institution_admin, role=org_admin, organization=university)
        migration.forwards(apps, None)
        self.assertEqual(RoleAssignment.objects.get(user=organizer).role.code, 'ORGANIZER')
        self.assertEqual(RoleAssignment.objects.get(user=staff).role.code, 'ORG_ADMIN')
        self.assertEqual(RoleAssignment.objects.get(user=institution_admin).role.code, 'ORG_ADMIN')
