"""Race-condition tests. They need real concurrent transactions, so they run
only against PostgreSQL (e.g. `docker compose run --rm web python manage.py test`)."""
import json
import threading
import unittest

from django.db import connection
from django.test import TransactionTestCase, override_settings

from core import audit
from core.tests.factories import add_position, institutional_setup, make_event, open_institutional
from elections.casting import AlreadyVoted, CastError, cast_ballot, issue_authorization
from elections.models import Ballot, VoteAuthorization, Voter
from voting.models import Event, VoteTransaction


def run_parallel(func, count=10):
    barrier = threading.Barrier(count)
    outcomes = []
    lock = threading.Lock()

    def worker(i):
        try:
            barrier.wait()
            result = func(i)
            with lock:
                outcomes.append(('ok', result))
        except Exception as exc:  # noqa: BLE001
            with lock:
                outcomes.append(('error', exc))
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


@unittest.skipUnless(connection.vendor == 'postgresql', 'Concurrency tests require PostgreSQL row locks')
class BallotRaceTests(TransactionTestCase):
    def setUp(self):
        self.s = institutional_setup()
        self.event = open_institutional(self.s['event'], self.s['admin'], self.s['reviewer'])
        self.payload = {str(self.s['president'].pk): [self.s['president_candidates'][0].pk], str(self.s['senate'].pk): []}

    def test_same_token_submitted_ten_times_concurrently(self):
        token, _ = issue_authorization(self.event, self.s['voters'][0], 'CODE')
        outcomes = run_parallel(lambda i: cast_ballot(Event.objects.get(pk=self.event.pk), token, self.payload))
        successes = [o for o in outcomes if o[0] == 'ok']
        self.assertEqual(len(successes), 1, outcomes)
        self.assertTrue(all(isinstance(o[1], (AlreadyVoted, CastError)) for o in outcomes if o[0] == 'error'))
        self.assertEqual(Ballot.objects.count(), 1)

    def test_same_voter_racing_through_sign_in_and_cast(self):
        voter_id = self.s['voters'][1].pk

        def attempt(i):
            event = Event.objects.get(pk=self.event.pk)
            token, _ = issue_authorization(event, Voter.objects.get(pk=voter_id), 'CODE')
            return cast_ballot(event, token, self.payload)

        outcomes = run_parallel(attempt)
        self.assertEqual(len([o for o in outcomes if o[0] == 'ok']), 1, outcomes)
        self.assertEqual(Ballot.objects.count(), 1)
        self.assertEqual(VoteAuthorization.objects.filter(voter_id=voter_id, status='CONSUMED').count(), 1)
        self.assertEqual(Voter.objects.get(pk=voter_id).status, Voter.Status.VOTED)

    def test_concurrent_audit_appends_keep_the_chain_intact(self):
        run_parallel(lambda i: audit.record('RACE_TEST', summary=f'writer {i}', organization_id=self.s['org'].pk), 12)
        ok, count, _, message = audit.verify_chain(audit.chain_for(self.s['org'].pk))
        self.assertTrue(ok, message)


@unittest.skipUnless(connection.vendor == 'postgresql', 'Concurrency tests require PostgreSQL row locks')
@override_settings(PAYMENTS_FAKE_GATEWAY=True, DEBUG=True)
class PaymentRaceTests(TransactionTestCase):
    def test_concurrent_webhook_replays_credit_once(self):
        from django.test import RequestFactory

        from payments import paystack, service

        event = make_event(institutional=False, status=Event.Status.OPEN)
        _, (alice, *_rest) = add_position(event, 'Best')
        request = RequestFactory().post('/', HTTP_USER_AGENT='Mozilla/5.0')
        payment = service.initiate_vote_payment(request, event, alice, votes=4, email='fan@gmail.com')
        store = paystack.fake_store(payment.reference)
        store['status'] = 'success'
        paystack.fake_set(payment.reference, store)
        data = paystack.verify(payment.reference)

        def deliver(i):
            # Distinct bodies defeat the payload-hash dedupe so the row lock is what's tested.
            body = json.dumps({'event': 'charge.success', 'data': data, 'delivery': i}).encode()
            return service.handle_webhook(body, paystack.signature_for(body))

        outcomes = run_parallel(deliver)
        self.assertTrue(all(o[0] == 'ok' for o in outcomes), outcomes)
        self.assertEqual(VoteTransaction.objects.filter(payment=payment).count(), 1)
        self.assertEqual(VoteTransaction.objects.get(payment=payment).number_of_votes, 4)

    def test_concurrent_identical_idempotency_keys_create_one_payment(self):
        from django.test import RequestFactory

        from payments import service
        from payments.models import Payment

        event = make_event(institutional=False, status=Event.Status.OPEN)
        _, (alice, *_rest) = add_position(event, 'Best')

        def create(i):
            request = RequestFactory().post('/', HTTP_USER_AGENT='Mozilla/5.0')
            return service.initiate_vote_payment(request, Event.objects.get(pk=event.pk), alice, votes=1,
                                                 email='fan@gmail.com', idempotency_key='same-key-123').pk

        outcomes = run_parallel(create)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(len({o[1] for o in outcomes if o[0] == 'ok'}), 1, outcomes)
