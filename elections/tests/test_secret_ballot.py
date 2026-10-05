"""End-to-end institutional election at the service layer:
setup -> review -> approval -> open -> vote -> close -> tally -> certify -> publish -> verify."""
import json

from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core import crypto
from core.tests.factories import institutional_setup, open_institutional
from elections import lifecycle, results
from elections.ballot import BallotError
from elections.casting import AlreadyVoted, CastError, cast_ballot, issue_authorization
from elections.models import Ballot, ElectionResult, VoteAuthorization, Voter
from voting.models import Event


class SecretBallotTests(TestCase):
    def setUp(self):
        self.s = institutional_setup()
        self.event = open_institutional(self.s['event'], self.s['admin'], self.s['reviewer'])

    def vote(self, voter, president, senate):
        token, _ = issue_authorization(self.event, voter, 'CODE')
        return cast_ballot(self.event, token, {str(self.s['president'].pk): president, str(self.s['senate'].pk): senate})

    def test_election_reached_open_with_key_snapshot_and_freezes(self):
        self.assertEqual(self.event.status, Event.Status.OPEN)
        self.assertTrue(hasattr(self.event, 'ballot_key'))
        self.assertTrue(self.event.ballot_frozen and self.event.candidates_frozen and self.event.voter_list_frozen)
        snapshot = self.event.config_snapshots.first()
        from elections.integrity import verify_snapshot

        self.assertTrue(verify_snapshot(snapshot))

    def test_ballot_is_encrypted_and_unlinkable(self):
        alice, bob = self.s['president_candidates'][:2]
        receipt = self.vote(self.s['voters'][0], [alice.pk], [self.s['senate_candidates'][0].pk])
        ballot = Ballot.objects.get()
        # No voter, token, authorization or timestamp columns exist on the ballot.
        columns = {f.column for f in Ballot._meta.fields}
        self.assertEqual(columns, {'id', 'election_id', 'ciphertext', 'tracker', 'style_hash', 'constituency_id'})
        self.assertNotIn('Alice', ballot.ciphertext)
        self.assertNotIn('selections', ballot.ciphertext)
        self.assertEqual(receipt['tracker'], ballot.tracker)
        self.assertEqual(ballot.tracker, crypto.sha256_hex(ballot.ciphertext))
        voter = Voter.objects.get(pk=self.s['voters'][0].pk)
        self.assertEqual(voter.status, Voter.Status.VOTED)
        self.assertIsNone(voter.credential_ciphertext)
        auth = VoteAuthorization.objects.get(voter=voter)
        self.assertEqual(auth.status, VoteAuthorization.Status.CONSUMED)
        # The audit trail records participation but never the tracker.
        from core.models import AuditEvent

        voted = AuditEvent.objects.get(event_type='VOTER_VOTED')
        self.assertNotIn(ballot.tracker, json.dumps(voted.metadata) + voted.summary)

    def test_identical_choices_produce_unrelated_ciphertexts(self):
        alice = self.s['president_candidates'][0]
        senate = [self.s['senate_candidates'][0].pk]
        first = self.vote(self.s['voters'][0], [alice.pk], senate)
        second = self.vote(self.s['voters'][1], [alice.pk], senate)
        self.assertNotEqual(first['tracker'], second['tracker'])

    def test_double_voting_and_token_reuse_rejected(self):
        voter = self.s['voters'][0]
        token, _ = issue_authorization(self.event, voter, 'CODE')
        payload = {str(self.s['president'].pk): [self.s['president_candidates'][0].pk],
                   str(self.s['senate'].pk): [self.s['senate_candidates'][0].pk]}
        cast_ballot(self.event, token, payload)
        with self.assertRaises(AlreadyVoted):
            cast_ballot(self.event, token, payload)
        with self.assertRaises(AlreadyVoted):
            issue_authorization(self.event, Voter.objects.get(pk=voter.pk), 'CODE')
        self.assertEqual(Ballot.objects.count(), 1)

    def test_reissuing_a_token_revokes_the_previous_one(self):
        voter = self.s['voters'][0]
        old, _ = issue_authorization(self.event, voter, 'CODE')
        new, _ = issue_authorization(self.event, voter, 'CODE')
        payload = {str(self.s['president'].pk): [self.s['president_candidates'][0].pk], str(self.s['senate'].pk): []}
        with self.assertRaises(CastError):
            cast_ballot(self.event, old, payload)
        cast_ballot(self.event, new, payload)

    def test_invalid_ballot_is_rejected_atomically(self):
        token, _ = issue_authorization(self.event, self.s['voters'][0], 'CODE')
        with self.assertRaises(BallotError):
            cast_ballot(self.event, token, {str(self.s['president'].pk): [999999]})
        self.assertEqual(Ballot.objects.count(), 0)
        self.assertEqual(Voter.objects.get(pk=self.s['voters'][0].pk).status, Voter.Status.ELIGIBLE)

    def test_paused_and_suspended_cannot_vote(self):
        lifecycle.transition(self.event, 'pause', actor=self.s['admin'])
        with self.assertRaises(CastError):
            issue_authorization(Event.objects.get(pk=self.event.pk), self.s['voters'][0], 'CODE')
        lifecycle.transition(self.event, 'resume', actor=self.s['admin'])
        voter = self.s['voters'][1]
        voter.status = Voter.Status.SUSPENDED
        voter.save()
        with self.assertRaises(CastError):
            issue_authorization(Event.objects.get(pk=self.event.pk), voter, 'CODE')

    def test_full_results_workflow(self):
        alice, bob, _ = self.s['president_candidates']
        d, e, _ = self.s['senate_candidates']
        self.vote(self.s['voters'][0], [alice.pk], [d.pk, e.pk])
        self.vote(self.s['voters'][1], [alice.pk], [d.pk])
        self.vote(self.s['voters'][2], [bob.pk], [])
        with self.captureOnCommitCallbacks(execute=True):
            lifecycle.transition(self.event, 'close', actor=self.s['admin'])
        self.event.refresh_from_db()
        # Automatic tally ran on close (Celery eager in tests).
        self.assertEqual(self.event.status, Event.Status.TALLYING)
        official = self.event.results.get(kind=ElectionResult.Kind.OFFICIAL)
        positions = {p['position_id']: p for p in official.data['positions']}
        president = positions[self.s['president'].pk]
        self.assertEqual(president['winners'], [alice.pk])
        self.assertEqual({c['candidate_id']: c['votes'] for c in president['candidates']}[alice.pk], 2)
        senate = positions[self.s['senate'].pk]
        self.assertEqual(senate['abstentions'], 1)
        self.assertEqual(official.data['turnout']['votes_cast'], 3)
        self.assertEqual(official.data['turnout']['turnout_percentage'], 100.0)

        certification = results.approve_and_certify(official, self.s['reviewer'], 'Checked')
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.CERTIFIED)
        self.assertTrue(crypto.verify_signature(crypto.canonical_json(certification.payload), certification.signature,
                                                certification.public_key))
        self.assertIsNone(results.public_results(self.event))  # not public before publication
        lifecycle.transition(self.event, 'publish', actor=self.s['reviewer'])
        self.event.refresh_from_db()
        public = results.public_results(self.event)
        self.assertTrue(public['certified'])

        bundle = results.verification_bundle(self.event)
        checks = results.verify_bundle(json.loads(json.dumps(bundle)))
        self.assertTrue(checks and all(ok for _, ok, _ in checks), checks)
        tracker = bundle['bulletin_board']['trackers'][0]
        proof = results.tracker_proof(self.event, tracker)
        self.assertTrue(proof['included'])
        self.assertTrue(crypto.verify_merkle_proof(tracker, proof['proof'], proof['root']))

        recount = results.recount(self.event, self.s['reviewer'], reason='Audit')
        self.assertTrue(recount.matches)

    def test_tampered_ballot_counts_as_invalid(self):
        self.vote(self.s['voters'][0], [self.s['president_candidates'][0].pk], [])
        if connection.vendor == 'postgresql':
            self.skipTest('Ballots are protected by an append-only trigger on PostgreSQL.')
        with connection.cursor() as cursor:
            cursor.execute("UPDATE elections_ballot SET ciphertext = 'AAAA' || substr(ciphertext, 5)")
        with self.captureOnCommitCallbacks(execute=True):
            lifecycle.transition(self.event, 'close', actor=self.s['admin'])
        official = self.event.results.get()
        self.assertEqual(official.data['turnout']['invalid_ballots'], 1)
        self.assertEqual(official.data['turnout']['ballots_counted'], 0)

    def test_separation_of_duties_when_dual_approval_required(self):
        s = institutional_setup(dual=True, prefix='d')
        event = s['event']
        lifecycle.transition(event, 'submit', actor=s['admin'])
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.transition(event, 'approve', actor=s['admin'])
        lifecycle.transition(event, 'approve', actor=s['reviewer'])
        lifecycle.transition(event, 'schedule', actor=s['admin'])
        lifecycle.transition(event, 'open', actor=s['admin'])
        token, _ = issue_authorization(event, s['voters'][0], 'CODE')
        cast_ballot(event, token, {str(s['president'].pk): [s['president_candidates'][0].pk], str(s['senate'].pk): []})
        with self.captureOnCommitCallbacks(execute=True):
            lifecycle.transition(event, 'close', actor=s['admin'])
        official = event.results.get()
        official.tallied_by = s['reviewer']
        official.save(update_fields=['tallied_by'])
        with self.assertRaises(results.ResultsError):
            results.approve_and_certify(official, s['reviewer'])

    def test_permissions_enforced_on_transitions(self):
        from core.tests.factories import make_user

        outsider = make_user('outsider')
        with self.assertRaises(PermissionDenied):
            lifecycle.transition(self.event, 'pause', actor=outsider)

    def test_automatic_close_on_tick(self):
        Event.objects.filter(pk=self.event.pk).update(end_date=timezone.now() - timezone.timedelta(seconds=1))
        event = lifecycle.tick(Event.objects.get(pk=self.event.pk))
        self.assertIn(event.status, (Event.Status.CLOSED, Event.Status.TALLYING))
