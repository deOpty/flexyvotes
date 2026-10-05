import re

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import AuditEvent
from core.tests.factories import PASSWORD, institutional_setup, make_org, make_user, open_institutional
from elections import lifecycle
from elections.models import Ballot, Dispute, Voter
from voting.models import Event


def otp_from_mail():
    body = mail.outbox[-1].body
    return re.search(r'\b(\d{6})\b', body).group(1)


class VoterWebFlowTests(TestCase):
    def setUp(self):
        self.s = institutional_setup()
        self.event = open_institutional(self.s['event'], self.s['admin'], self.s['reviewer'])
        self.voter = self.s['voters'][0]
        self.code = self.s['codes'][self.voter.pk]

    def sign_in(self, code=None, identifier=None):
        return self.client.post(reverse('elections:vote_start', args=[self.event.pk]),
                                {'action': 'code', 'code': code or self.code, 'identifier': identifier or ''})

    def ballot_post(self):
        alice = self.s['president_candidates'][0]
        dede = self.s['senate_candidates'][0]
        return {f'pos_{self.s["president"].pk}': str(alice.pk), f'pos_{self.s["senate"].pk}': [str(dede.pk)]}

    def test_complete_voting_journey(self):
        response = self.sign_in()
        self.assertRedirects(response, reverse('elections:vote_ballot', args=[self.event.pk]))
        response = self.client.get(reverse('elections:vote_ballot', args=[self.event.pk]))
        self.assertContains(response, 'Alice')
        self.assertContains(response, 'nonce=')
        response = self.client.post(reverse('elections:vote_ballot', args=[self.event.pk]), self.ballot_post())
        self.assertRedirects(response, reverse('elections:vote_review', args=[self.event.pk]))
        review = self.client.get(reverse('elections:vote_review', args=[self.event.pk]))
        self.assertContains(review, 'Alice')
        submission = review.context['submission_id']
        # Missing confirmation tick -> nothing is cast.
        response = self.client.post(reverse('elections:vote_review', args=[self.event.pk]),
                                    {'submission_id': submission, 'action': 'cast'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Ballot.objects.count(), 0)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('elections:vote_review', args=[self.event.pk]),
                                        {'submission_id': submission, 'action': 'cast', 'confirm': 'on'})
        self.assertRedirects(response, reverse('elections:vote_receipt', args=[self.event.pk]))
        receipt = self.client.get(reverse('elections:vote_receipt', args=[self.event.pk]))
        tracker = Ballot.objects.get().tracker
        self.assertContains(receipt, tracker)
        self.assertEqual(Voter.objects.get(pk=self.voter.pk).status, Voter.Status.VOTED)
        # Confirmation email is sent without the tracker.
        confirmation = [m for m in mail.outbox if 'recorded' in m.subject]
        self.assertTrue(confirmation)
        self.assertNotIn(tracker, confirmation[0].body)
        # Replayed form submission does not create a second ballot.
        self.client.post(reverse('elections:vote_review', args=[self.event.pk]),
                         {'submission_id': submission, 'action': 'cast', 'confirm': 'on'})
        self.assertEqual(Ballot.objects.count(), 1)
        # Signing in again is refused.
        response = self.sign_in()
        self.assertContains(response, 'already voted', status_code=400)

    def test_wrong_code_gives_generic_error_and_is_audited(self):
        response = self.sign_in(code='WRONGCODE1')
        self.assertContains(response, 'could not verify', status_code=400)
        self.assertTrue(AuditEvent.objects.filter(event_type='VOTER_AUTH_FAILED').exists())

    @override_settings(VOTER_LOGIN_PER_IP_PER_MIN=15)
    def test_rate_limited_after_repeated_attempts(self):
        statuses = [self.sign_in(code=f'BAD{i:07d}').status_code for i in range(17)]
        self.assertEqual(statuses[-1], 429)

    def test_guessing_one_voters_code_is_limited_per_identifier(self):
        Event.objects.filter(pk=self.event.pk).update(code_voting_mode=Event.CodeVotingMode.STUDENT_ID)
        statuses = [self.sign_in(code=f'BAD{i:07d}', identifier=self.voter.identifier).status_code for i in range(11)]
        self.assertEqual(statuses[:10], [400] * 10)
        self.assertEqual(statuses[10], 429)

    def test_code_only_voters_do_not_share_a_rate_limit(self):
        """Regression: with no identifier, every voter used to share one
        per-identifier counter, so the 11th sign-in in 10 minutes failed for
        everyone. A whole campus behind one NAT IP must be able to vote."""
        for i in range(12):
            self.client.post(reverse('elections:vote_start', args=[self.event.pk]),
                             {'action': 'code', 'code': f'WRONG{i:05d}', 'identifier': ''})
        response = self.sign_in()
        self.assertRedirects(response, reverse('elections:vote_ballot', args=[self.event.pk]))

    def test_student_id_mode_requires_matching_identifier(self):
        Event.objects.filter(pk=self.event.pk).update(code_voting_mode=Event.CodeVotingMode.STUDENT_ID)
        self.assertEqual(self.sign_in(identifier='ST999').status_code, 400)
        response = self.sign_in(identifier=self.voter.identifier.lower())
        self.assertEqual(response.status_code, 302)

    def test_second_factor_required(self):
        Event.objects.filter(pk=self.event.pk).update(require_second_factor=True)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.sign_in()
        self.assertRedirects(response, reverse('elections:vote_otp', args=[self.event.pk]))
        # The ballot is not reachable before the second factor.
        self.assertEqual(self.client.get(reverse('elections:vote_ballot', args=[self.event.pk])).status_code, 302)
        response = self.client.post(reverse('elections:vote_otp', args=[self.event.pk]), {'code': '000000'})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(reverse('elections:vote_otp', args=[self.event.pk]), {'code': otp_from_mail()})
        self.assertRedirects(response, reverse('elections:vote_ballot', args=[self.event.pk]))
        self.assertIsNotNone(Voter.objects.get(pk=self.voter.pk).email_verified_at)

    def test_email_otp_login_without_enumeration(self):
        Event.objects.filter(pk=self.event.pk).update(auth_methods=['EMAIL_OTP'])
        unknown = self.client.post(reverse('elections:vote_start', args=[self.event.pk]),
                                   {'action': 'otp', 'identifier': 'NOBODY'})
        self.assertRedirects(unknown, reverse('elections:vote_otp', args=[self.event.pk]))
        with self.captureOnCommitCallbacks(execute=True):
            known = self.client.post(reverse('elections:vote_start', args=[self.event.pk]),
                                     {'action': 'otp', 'identifier': self.voter.identifier})
        self.assertRedirects(known, reverse('elections:vote_otp', args=[self.event.pk]))
        response = self.client.post(reverse('elections:vote_otp', args=[self.event.pk]), {'code': otp_from_mail()})
        self.assertRedirects(response, reverse('elections:vote_ballot', args=[self.event.pk]))

    def test_lost_code_is_only_sent_to_roll_email(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('retrieve_voting_code', args=[self.event.pk]), {'student_id': self.voter.identifier})
        self.assertEqual(mail.outbox[-1].to, [self.voter.email])
        # The old code no longer works; the new one does.
        self.assertEqual(self.sign_in().status_code, 400)
        new_code = re.search(r'access code: ([A-Z0-9]+)', mail.outbox[-1].body).group(1)
        self.assertEqual(self.sign_in(code=new_code).status_code, 302)

    def test_self_registration_with_email_verification(self):
        Event.objects.filter(pk=self.event.pk).update(allow_self_registration=True, registration_email_domains='uni.edu',
                                                      voter_list_frozen=False)
        url = reverse('elections:vote_register', args=[self.event.pk])
        response = self.client.post(url, {'name': 'New Person', 'identifier': 'NEW1', 'email': 'new@gmail.com'})
        self.assertEqual(response.status_code, 400)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {'name': 'New Person', 'identifier': 'NEW1', 'email': 'new@uni.edu'})
        self.assertRedirects(response, url)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {'action': 'verify', 'code': otp_from_mail()})
        self.assertRedirects(response, reverse('elections:vote_ballot', args=[self.event.pk]))
        voter = Voter.objects.get(identifier='NEW1')
        self.assertEqual(voter.status, Voter.Status.VERIFIED)

    def test_closed_election_rejects_new_sessions(self):
        lifecycle.transition(self.event, 'close', actor=self.s['admin'])
        response = self.sign_in()
        self.assertEqual(response.status_code, 302)
        ballot = self.client.get(reverse('elections:vote_ballot', args=[self.event.pk]), follow=True)
        self.assertContains(ballot, 'closed')


class ConsoleAndPublicPageTests(TestCase):
    def setUp(self):
        self.s = institutional_setup()
        self.event = self.s['event']
        self.client.login(username='admin1', password=PASSWORD)

    def console_urls(self):
        pk = self.event.pk
        names = ['console_overview', 'console_settings', 'console_ballot', 'console_ballot_preview', 'console_voters',
                 'console_eligibility', 'console_results', 'console_trustees', 'console_integrity', 'console_audit',
                 'console_monitor', 'console_monitor_data']
        return [reverse(f'elections:{n}', args=[pk]) for n in names] + [
            reverse('dashboard'), reverse('elections:console_list'), reverse('console:approvals'), reverse('console:audit'),
            reverse('console:team'), reverse('console:reports'), reverse('console:notifications'),
            reverse('elections:constituencies', args=[self.s['org'].pk]), reverse('console:organization', args=[self.s['org'].pk]),
            reverse('payments:console_list'), reverse('payments:revenue'), reverse('payments:reconciliation'),
            reverse('payments:refunds'), reverse('fraud:alerts'), reverse('billing:home'), reverse('account:security'),
            reverse('edit_event', args=[pk]), reverse('event_analytics', args=[pk]), reverse('create_event'),
            reverse('elections:console_voters_export', args=[pk]),
        ]

    def assert_all_ok(self):
        for url in self.console_urls():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, f'{url} -> {response.status_code}')

    def test_console_pages_render_through_the_lifecycle(self):
        self.assert_all_ok()
        open_institutional(self.event, self.s['admin'], self.s['reviewer'])
        self.assert_all_ok()
        lifecycle.transition(self.event, 'close', actor=self.s['admin'])
        from elections.results import run_tally

        run_tally(self.event, actor=self.s['admin'])
        self.assert_all_ok()
        for fmt in ('csv', 'xlsx', 'pdf'):
            response = self.client.get(reverse('elections:console_results_export', args=[self.event.pk, fmt]))
            self.assertEqual(response.status_code, 200, fmt)

    def test_other_tenant_cannot_see_console(self):
        other = make_user('otheradmin')
        make_org('Other Org', admin=other)
        self.client.logout()
        self.client.login(username='otheradmin', password=PASSWORD)
        for name in ('console_overview', 'console_voters', 'console_results', 'console_audit'):
            response = self.client.get(reverse(f'elections:{name}', args=[self.event.pk]))
            self.assertEqual(response.status_code, 403, name)
        self.assertNotContains(self.client.get(reverse('elections:console_list')), self.event.title)
        self.assertTrue(AuditEvent.objects.filter(event_type='ACCESS_DENIED').exists())

    def test_public_pages(self):
        self.client.logout()
        # Draft elections are not public.
        self.assertEqual(self.client.get(reverse('event_detail', args=[self.event.pk])).status_code, 404)
        open_institutional(self.event, self.s['admin'], self.s['reviewer'])
        for url in (reverse('home'), reverse('event_detail', args=[self.event.pk]),
                    reverse('elections:candidates', args=[self.event.pk]),
                    reverse('elections:candidate_profile', args=[self.event.pk, self.s['president_candidates'][0].pk]),
                    reverse('elections:results', args=[self.event.pk]), reverse('elections:verify', args=[self.event.pk]),
                    reverse('elections:verify_bundle', args=[self.event.pk]), reverse('elections:results_index'),
                    reverse('elections:vote_start', args=[self.event.pk]), reverse('accessibility')):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
        response = self.client.get(reverse('elections:verify', args=[self.event.pk]) + '?tracker=' + 'a' * 64)
        self.assertContains(response, 'No ballot with that tracker')
        response = self.client.post(reverse('elections:file_dispute', args=[self.event.pk]), {
            'name': 'Kofi', 'email': 'kofi@example.com', 'subject': 'Queue', 'description': 'Long queue', 'role': 'VOTER',
            'category': 'CONDUCT'})
        self.assertEqual(response.status_code, 302)
        dispute = Dispute.objects.get()
        self.assertEqual(dispute.filer_email, 'kofi@example.com')
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute('SELECT filer_email FROM elections_dispute')
            self.assertTrue(cursor.fetchone()[0].startswith('fv1$'))

    def test_lifecycle_transition_endpoint_and_separation(self):
        url = reverse('elections:console_transition', args=[self.event.pk])
        self.client.post(url, {'action': 'submit'})
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.REVIEW)
        response = self.client.post(url, {'action': 'open'}, follow=True)
        self.assertContains(response, 'Cannot')
