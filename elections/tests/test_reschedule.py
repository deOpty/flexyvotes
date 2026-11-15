"""Administrators change voting times at any stage and restart ended voting."""
import zoneinfo
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from core.tests.factories import (PASSWORD, add_position, grant, institutional_setup, make_event, make_org, make_user,
                                  open_institutional)
from elections import lifecycle, results
from elections.casting import AlreadyVoted, cast_ballot, issue_authorization
from elections.models import ElectionResult
from voting.models import Candidate, Event


def window(start_minutes, end_minutes):
    now = timezone.now()
    return now + timedelta(minutes=start_minutes), now + timedelta(minutes=end_minutes)


def form(event, start, end, reason='Polling stations opened late'):
    """The schedule form takes date + time in the election's own time zone."""
    zone = zoneinfo.ZoneInfo(event.timezone)
    start, end = start.astimezone(zone), end.astimezone(zone)
    return {'start_date': start.strftime('%Y-%m-%d'), 'start_time': start.strftime('%H:%M'),
            'end_date': end.strftime('%Y-%m-%d'), 'end_time': end.strftime('%H:%M'), 'reason': reason}


class PaidRescheduleTests(TestCase):
    def setUp(self):
        self.admin = make_user('platform', staff=True, superuser=True)
        self.org = make_org('Awards Org')
        self.event = make_event(org=self.org, organizer=self.admin, institutional=False, status=Event.Status.OPEN,
                                timezone='Africa/Lagos')
        self.position, _ = add_position(self.event, 'Best Artist', candidates=('A', 'B'))
        self.client.login(username='platform', password=PASSWORD)
        self.url = reverse('elections:console_schedule', args=[self.event.pk])

    def test_schedule_page_renders(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Change voting times')

    def test_admin_extends_open_voting(self):
        start, end = window(-120, 3 * 24 * 60)
        response = self.client.post(self.url, form(self.event, start, end))
        self.assertRedirects(response, reverse('elections:console_overview', args=[self.event.pk]))
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.OPEN)
        self.assertEqual(self.event.end_date.replace(second=0, microsecond=0), end.replace(second=0, microsecond=0))
        entry = AuditEvent.objects.get(event_type='ELECTION_RESCHEDULED', election_id=self.event.pk)
        self.assertEqual(entry.reason, 'Polling stations opened late')
        self.assertIn('end_date', entry.changes)

    def test_live_change_needs_reason_and_past_start(self):
        start, end = window(-60, 600)
        with self.assertRaisesMessage(lifecycle.LifecycleError, 'Give a reason'):
            lifecycle.reschedule(self.event, start=start, end=end, actor=self.admin, reason='')
        start, end = window(60, 600)
        with self.assertRaisesMessage(lifecycle.LifecycleError, 'cannot move into the future'):
            lifecycle.reschedule(self.event, start=start, end=end, actor=self.admin, reason='x')
        start, end = window(-60, -10)
        with self.assertRaisesMessage(lifecycle.LifecycleError, 'must be in the future'):
            lifecycle.reschedule(self.event, start=start, end=end, actor=self.admin, reason='x')
        start, _ = window(-60, 0)
        with self.assertRaisesMessage(lifecycle.LifecycleError, 'after the start'):
            lifecycle.reschedule(self.event, start=start, end=start, actor=self.admin, reason='x')

    def test_paused_election_past_its_end_is_restarted(self):
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.PAUSED,
                                                      end_date=timezone.now() - timedelta(minutes=5))
        start, end = window(-180, 120)
        self.client.post(self.url, form(self.event, start, end, 'Power cut'))
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.OPEN)
        self.assertTrue(self.event.accepting_votes())

    def test_restart_ended_paid_voting_after_the_period(self):
        Event.objects.filter(pk=self.event.pk).update(end_date=timezone.now() - timedelta(minutes=1))
        self.event = lifecycle.tick(Event.objects.get(pk=self.event.pk))
        self.assertEqual(self.event.status, Event.Status.CLOSED)
        self.assertFalse(self.event.accepting_votes())
        self.assertTrue(self.event.candidates_frozen)
        self.assertContains(self.client.get(self.url), 'Restart voting')

        start, end = window(-180, 24 * 60)
        self.client.post(self.url, form(self.event, start, end, 'Organiser asked for one more day'))
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.OPEN)
        self.assertIsNone(self.event.closed_at)
        self.assertTrue(self.event.accepting_votes())
        # Paid voting is unfrozen again so contestants can still be added.
        self.assertFalse(self.event.candidates_frozen)
        self.client.post(reverse('add_candidate', args=[self.event.pk]), {'name': 'Late entry',
                                                                          'category': self.position.pk})
        self.assertTrue(Candidate.objects.filter(event=self.event, name='Late entry').exists())
        self.assertTrue(AuditEvent.objects.filter(event_type='ELECTION_RESTART', election_id=self.event.pk).exists())

    def test_restart_with_future_start_schedules_then_opens(self):
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.CLOSED, closed_at=timezone.now(),
                                                      end_date=timezone.now() - timedelta(hours=1))
        start, end = window(60, 600)
        event = lifecycle.reschedule(Event.objects.get(pk=self.event.pk), start=start, end=end, actor=self.admin,
                                     reason='Second round tomorrow')
        self.assertEqual(event.status, Event.Status.SCHEDULED)
        event = lifecycle.tick(event, now=start + timedelta(minutes=1))
        self.assertEqual(event.status, Event.Status.OPEN)

    def test_not_yet_open_change_keeps_approval(self):
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.APPROVED)
        start, end = window(2 * 24 * 60, 3 * 24 * 60)
        event = lifecycle.reschedule(Event.objects.get(pk=self.event.pk), start=start, end=end, actor=self.admin)
        self.assertEqual(event.status, Event.Status.APPROVED)
        self.assertEqual(event.config_snapshots.filter(reason='RESCHEDULED').count(), 1)

    def test_archived_is_read_only(self):
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.ARCHIVED)
        start, end = window(-60, 600)
        with self.assertRaisesMessage(lifecycle.LifecycleError, 'read-only'):
            lifecycle.reschedule(Event.objects.get(pk=self.event.pk), start=start, end=end, actor=self.admin,
                                 reason='x')

    def test_ended_voting_cannot_restart_into_the_past(self):
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.CLOSED)
        start, end = window(-600, -60)
        response = self.client.post(self.url, form(self.event, start, end), follow=True)
        self.assertContains(response, 'must be in the future')
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.CLOSED)


class SystemOnlyActionTests(TestCase):
    def test_system_actions_cannot_be_requested_directly(self):
        import json

        from core.auth import create_api_token

        admin = make_user('platform', staff=True, superuser=True)
        event = make_event(org=make_org('Org'), organizer=admin, institutional=False, status=Event.Status.CLOSED)
        self.client.force_login(admin)
        for action in ('restart', 'restart_scheduled', 'reopen_voting', 'certify'):
            self.client.post(reverse('elections:console_transition', args=[event.pk]), {'action': action})
            event.refresh_from_db()
            self.assertEqual(event.status, Event.Status.CLOSED, action)
        raw, _ = create_api_token(admin, 'tests')
        response = self.client.post(f'/api/v1/elections/{event.pk}/transitions', json.dumps({'action': 'restart'}),
                                    content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {raw}')
        self.assertEqual(response.status_code, 409)
        event.refresh_from_db()
        self.assertEqual(event.status, Event.Status.CLOSED)


class ReschedulePermissionTests(TestCase):
    def test_only_admins_hold_reschedule(self):
        org_admin = make_user('orgadmin')
        election_admin = make_user('eadmin')
        officer = make_user('officer')
        organizer = make_user('organizer')
        org = make_org('Uni', admin=org_admin)
        grant(election_admin, 'ELECTION_ADMIN', org)
        grant(officer, 'ELECTION_OFFICER', org)
        grant(organizer, 'ORGANIZER', org)
        event = make_event(org=org, organizer=organizer, institutional=False, status=Event.Status.CLOSED)
        start, end = window(-60, 600)
        for user in (officer, organizer):
            with self.assertRaises(PermissionDenied):
                lifecycle.reschedule(event, start=start, end=end, actor=user, reason='x')
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('elections:console_schedule', args=[event.pk])).status_code, 403)
        event = lifecycle.reschedule(event, start=start, end=end, actor=election_admin, reason='Re-run')
        self.assertEqual(event.status, Event.Status.OPEN)
        Event.objects.filter(pk=event.pk).update(status=Event.Status.CLOSED)
        event = lifecycle.reschedule(Event.objects.get(pk=event.pk), start=start, end=end, actor=org_admin,
                                     reason='Again')
        self.assertEqual(event.status, Event.Status.OPEN)


class InstitutionalRestartTests(TestCase):
    """Secret-ballot election: tallied, certified and published results are
    superseded on restart; earlier voters stay counted and cannot vote twice."""

    def setUp(self):
        self.s = institutional_setup()
        self.event = open_institutional(self.s['event'], self.s['admin'], self.s['reviewer'])

    def vote(self, voter):
        token, _ = issue_authorization(self.event, voter, 'CODE')
        return cast_ballot(self.event, token, {str(self.s['president'].pk): [self.s['president_candidates'][0].pk],
                                               str(self.s['senate'].pk): []})

    def close_and_tally(self):
        with self.captureOnCommitCallbacks(execute=True):
            lifecycle.transition(self.event, 'close', actor=self.s['admin'])
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.TALLYING)
        return self.event.results.get(kind=ElectionResult.Kind.OFFICIAL, status=ElectionResult.Status.PENDING_REVIEW)

    def test_restart_after_publication_and_retally(self):
        first, second, third = self.s['voters']
        self.vote(first)
        self.vote(second)
        official = self.close_and_tally()
        self.assertEqual(official.votes_cast, 2)
        certification = results.approve_and_certify(official, self.s['reviewer'], 'ok')
        lifecycle.transition(self.event, 'publish', actor=self.s['reviewer'])
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.PUBLISHED)

        start, end = window(-24 * 60, 120)
        with self.captureOnCommitCallbacks(execute=True):
            self.event = lifecycle.reschedule(self.event, start=start, end=end, actor=self.s['admin'],
                                              reason='Third campus could not vote')
        self.assertEqual(self.event.status, Event.Status.OPEN)
        self.assertIsNone(self.event.published_at)
        self.assertIsNone(results.public_results(self.event))
        certification.refresh_from_db()
        self.assertIsNotNone(certification.revoked_at)
        official.refresh_from_db()
        self.assertEqual(official.status, ElectionResult.Status.SUPERSEDED)
        entry = AuditEvent.objects.filter(event_type='ELECTION_RESCHEDULED', election_id=self.event.pk).first()
        self.assertEqual(entry.metadata['superseded_results'], [official.pk])

        # Earlier voters cannot vote again; the remaining voter can.
        with self.assertRaises(AlreadyVoted):
            self.vote(type(first).objects.get(pk=first.pk))
        self.vote(third)
        retally = self.close_and_tally()
        self.assertEqual(retally.votes_cast, 3)
        self.assertNotEqual(retally.pk, official.pk)
