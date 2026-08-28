import hashlib
import hmac
import json
import os
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Candidate, Category, Event, Ticket, TicketPurchase, VoteTransaction, VotingCode, hash_voting_code


def make_event(**kwargs):
    now = timezone.now()
    defaults = {
        'title': 'Test Event',
        'start_date': now,
        'end_date': now + timezone.timedelta(days=1),
    }
    defaults.update(kwargs)
    return Event.objects.create(**defaults)


class RegisterViewTests(TestCase):
    def test_weak_password_is_rejected(self):
        # Regression: register_view used to call create_user() directly,
        # bypassing AUTH_PASSWORD_VALIDATORS entirely.
        url = reverse('register')
        response = self.client.post(url, {
            'username': 'newuser', 'email': 'new@example.com', 'password': '123',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='newuser').exists())

    def test_strong_password_creates_user(self):
        # Regression: register_view previously always fell through to
        # re-render the form, even after a successful registration.
        url = reverse('register')
        response = self.client.post(url, {
            'username': 'newuser2', 'email': 'new2@example.com',
            'password': 'a-reasonably-strong-pw-93',
        })
        self.assertRedirects(response, reverse('home'))
        self.assertTrue(User.objects.filter(username='newuser2').exists())


class VotingCodeModelTests(TestCase):
    def test_default_code_is_unique_per_instance(self):
        # Regression: default used to be evaluated once at class-definition time,
        # so every code without an explicit value collided on the unique constraint.
        event = make_event()
        code1 = VotingCode.objects.create(event=event)
        code2 = VotingCode.objects.create(event=event)
        self.assertNotEqual(code1.code, code2.code)


class CastVoteWithCodeTests(TestCase):
    def setUp(self):
        self.event = make_event()
        self.candidate = Candidate.objects.create(event=self.event, name='Alice')

    def test_get_request_does_not_crash(self):
        # Regression: `candidate` was only defined inside the POST branch,
        # so a GET request raised UnboundLocalError.
        url = reverse('cast_vote_with_code', args=[self.candidate.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    def test_valid_code_casts_vote(self):
        voting_code = VotingCode.objects.create(event=self.event)
        url = reverse('cast_vote_with_code', args=[self.candidate.id])
        response = self.client.post(url, {'code': voting_code.code})
        self.assertEqual(response.status_code, 302)
        voting_code.refresh_from_db()
        self.assertTrue(voting_code.is_used)


class PaystackWebhookTests(TestCase):
    def test_invalid_signature_rejected(self):
        url = reverse('paystack_webhook')
        response = self.client.post(
            url, data=json.dumps({'event': 'charge.success', 'data': {}}),
            content_type='application/json',
            HTTP_X_PAYSTACK_SIGNATURE='not-the-real-signature',
        )
        self.assertEqual(response.status_code, 400)

    def test_malformed_json_with_valid_signature_returns_400(self):
        from django.conf import settings
        body = b'not-json'
        signature = hmac.new(
            settings.PAYSTACK_SECRET_KEY.encode('utf-8'), body, hashlib.sha512
        ).hexdigest()
        url = reverse('paystack_webhook')
        response = self.client.post(
            url, data=body, content_type='application/json',
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )
        self.assertEqual(response.status_code, 400)


class VoteSuccessPaymentBypassTests(TestCase):
    def setUp(self):
        self.event = make_event()
        self.candidate = Candidate.objects.create(event=self.event, name='Alice')
        self.transaction = VoteTransaction.objects.create(
            candidate=self.candidate, voter_email='v@example.com', amount=5,
            paystack_reference='REF-UNPAID-1', status='Pending', number_of_votes=5,
        )

    def test_unverified_payment_is_not_credited(self):
        # Regression: vote_success used to mark ANY Pending transaction as
        # Success just because the client hit this URL with its reference,
        # letting a voter get free votes by skipping payment entirely.
        with patch('voting.views.verify_paystack_transaction', return_value=False):
            url = reverse('vote_success') + '?reference=REF-UNPAID-1'
            self.client.get(url)

        self.transaction.refresh_from_db()
        self.assertEqual(self.transaction.status, 'Pending')

    def test_verified_payment_is_credited(self):
        with patch('voting.views.verify_paystack_transaction', return_value=True):
            url = reverse('vote_success') + '?reference=REF-UNPAID-1'
            self.client.get(url)

        self.transaction.refresh_from_db()
        self.assertEqual(self.transaction.status, 'Success')


class BuyTicketTests(TestCase):
    def setUp(self):
        self.event = make_event()
        self.ticket = Ticket.objects.create(
            event=self.event, name='VIP', price=10, quantity_available=1
        )

    def test_sold_out_ticket_is_rejected(self):
        TicketPurchase.objects.create(
            ticket=self.ticket, event=self.event, buyer_email='a@example.com',
            quantity=1, paystack_reference='TK-AAAA01', status='Success',
        )
        url = reverse('buy_ticket', args=[self.ticket.id])
        response = self.client.post(url, {
            'name': 'Bob', 'email': 'bob@example.com', 'quantity': 1,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TicketPurchase.objects.filter(buyer_email='bob@example.com').count(), 0)

    def test_unverified_ticket_payment_is_not_credited(self):
        # Regression: ticket_success used to mark ANY Pending purchase as
        # Success just because the client hit this URL, letting buyers get
        # a free ticket by skipping payment entirely.
        purchase = TicketPurchase.objects.create(
            ticket=self.ticket, event=self.event, buyer_email='c@example.com',
            quantity=1, paystack_reference='TK-UNPAID1', status='Pending',
        )
        with patch('voting.views.verify_paystack_transaction', return_value=False):
            self.client.get(reverse('ticket_success') + '?reference=TK-UNPAID1')

        purchase.refresh_from_db()
        self.assertEqual(purchase.status, 'Pending')


class ProcessScanAuthorizationTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user('organizer', password='pw')
        self.other_user = User.objects.create_user('rando', password='pw')
        self.event = make_event(organizer=self.organizer)

    def test_non_organizer_cannot_check_in_tickets(self):
        # Regression: process_scan had no organizer/staff check at all.
        self.client.login(username='rando', password='pw')
        url = reverse('process_scan', args=[self.event.id])
        response = self.client.post(
            url, data=json.dumps({'text': 'REF: TK-AAAA01'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_organizer_can_access(self):
        self.client.login(username='organizer', password='pw')
        url = reverse('process_scan', args=[self.event.id])
        response = self.client.post(
            url, data=json.dumps({'text': 'REF: TK-NOTFOUND'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)


class CodeHashingTests(TestCase):
    def test_code_is_never_stored_in_plaintext_after_use(self):
        event = make_event()
        candidate = Candidate.objects.create(event=event, name='Alice')
        voting_code = VotingCode.objects.create(event=event)
        raw_code = voting_code.code
        self.assertEqual(voting_code.code_hash, hash_voting_code(event.id, raw_code))

        url = reverse('cast_vote_with_code', args=[candidate.id])
        self.client.post(url, {'code': raw_code})
        voting_code.refresh_from_db()

        self.assertTrue(voting_code.is_used)
        self.assertEqual(voting_code.code, '')  # scrubbed once spent
        self.assertEqual(voting_code.code_hash, hash_voting_code(event.id, raw_code))  # hash survives for audit

    def test_lookup_works_purely_via_hash(self):
        event = make_event()
        voting_code = VotingCode.objects.create(event=event)
        found = VotingCode.objects.get(event=event, code_hash=hash_voting_code(event.id, voting_code.code))
        self.assertEqual(found.id, voting_code.id)


class BallotSecrecyTests(TestCase):
    def test_vote_transaction_never_embeds_code_or_identifier(self):
        event = make_event()
        candidate = Candidate.objects.create(event=event, name='Alice')
        voting_code = VotingCode.objects.create(event=event, voter_identifier='STD-SECRET-007')
        raw_code = voting_code.code

        url = reverse('cast_vote_with_code', args=[candidate.id])
        self.client.post(url, {'code': raw_code, 'identifier': 'STD-SECRET-007'})

        transaction = VoteTransaction.objects.get(candidate=candidate)
        self.assertNotIn(raw_code, transaction.voter_email)
        self.assertNotIn(raw_code, transaction.paystack_reference)
        self.assertNotIn('STD-SECRET-007', transaction.voter_email)
        self.assertNotIn('STD-SECRET-007', transaction.paystack_reference)


class BallotConcurrencyTests(TestCase):
    """Verifies the select_for_update + in-lock is_used re-check makes a
    voting code single-use against duplicate submissions/retries. True
    concurrent-lock contention needs a real multi-connection backend
    (Postgres); SQLite's select_for_update is a no-op, so this exercises the
    sequential double-submit path the atomic block guards against.
    """

    def test_same_code_cannot_cast_twice(self):
        event = make_event()
        category = Category.objects.create(event=event, name='President')
        candidate = Candidate.objects.create(event=event, name='Alice', category=category)
        voting_code = VotingCode.objects.create(event=event)
        raw_code = voting_code.code

        url = reverse('cast_ballot', args=[event.id])
        payload = json.dumps({'code': raw_code, 'votes': {str(category.id): [str(candidate.id)]}})

        first = self.client.post(url, data=payload, content_type='application/json')
        second = self.client.post(url, data=payload, content_type='application/json')

        self.assertEqual(first.json()['status'], 'success')
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()['status'], 'error')
        self.assertEqual(VoteTransaction.objects.filter(candidate=candidate).count(), 1)


class BallotRulesTests(TestCase):
    def setUp(self):
        self.event = make_event()
        self.category = Category.objects.create(event=self.event, name='President', max_select=1, allow_abstain=True)
        self.candidate_a = Candidate.objects.create(event=self.event, name='Alice', category=self.category)
        self.candidate_b = Candidate.objects.create(event=self.event, name='Bob', category=self.category)
        self.voting_code = VotingCode.objects.create(event=self.event)

    def cast(self, votes):
        url = reverse('cast_ballot', args=[self.event.id])
        return self.client.post(
            url, data=json.dumps({'code': self.voting_code.code, 'votes': votes}),
            content_type='application/json',
        )

    def test_exceeding_max_select_is_rejected(self):
        response = self.cast({str(self.category.id): [str(self.candidate_a.id), str(self.candidate_b.id)]})
        self.assertEqual(response.status_code, 400)
        self.voting_code.refresh_from_db()
        self.assertFalse(self.voting_code.is_used)

    def test_abstain_allowed_when_enabled(self):
        response = self.cast({str(self.category.id): []})
        self.assertEqual(response.json()['status'], 'success')
        self.assertEqual(VoteTransaction.objects.filter(candidate__category=self.category).count(), 0)

    def test_abstain_rejected_when_disallowed(self):
        self.category.allow_abstain = False
        self.category.save()
        response = self.cast({str(self.category.id): []})
        self.assertEqual(response.status_code, 400)


class RetrieveCodeEmailRedirectTests(TestCase):
    def test_resend_only_goes_to_email_on_file(self):
        event = make_event()
        voting_code = VotingCode.objects.create(
            event=event, voter_identifier='STD001', voter_email='real-owner@example.com'
        )
        old_hash = voting_code.code_hash

        url = reverse('retrieve_voting_code', args=[event.id])
        # Attacker-supplied "email" field from the old form is no longer
        # accepted at all - only voter_identifier is read.
        response = self.client.post(url, {'student_id': 'STD001', 'email': 'attacker@evil.com'})
        self.assertEqual(response.status_code, 302)

        from django.core import mail
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['real-owner@example.com'])

        voting_code.refresh_from_db()
        self.assertTrue(voting_code.is_used)  # old code invalidated
        self.assertEqual(voting_code.code_hash, old_hash)
        self.assertTrue(VotingCode.objects.filter(event=event, voter_identifier='STD001', is_used=False).exists())


class VotingLockTests(TestCase):
    def test_locked_event_rejects_ballot_even_before_end_date(self):
        event = make_event(voting_locked=True)
        candidate = Candidate.objects.create(event=event, name='Alice')
        voting_code = VotingCode.objects.create(event=event)

        url = reverse('cast_ballot', args=[event.id])
        response = self.client.post(
            url, data=json.dumps({'code': voting_code.code, 'votes': {}}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        voting_code.refresh_from_db()
        self.assertFalse(voting_code.is_used)


class LoginViewCrlfTests(TestCase):
    # Regression: a password copied out of a CRLF-terminated .env file (the
    # common case when it's edited on Windows) can carry a trailing \r/\n
    # onto the clipboard, which used to silently fail to match the stored
    # hash and look exactly like "wrong password".
    def setUp(self):
        User.objects.create_user('admin', password='ChangeMe-Strong-Pw-93')

    def test_trailing_crlf_on_password_is_tolerated(self):
        response = self.client.post(reverse('login'), {
            'username': 'admin', 'password': 'ChangeMe-Strong-Pw-93\r\n',
        })
        self.assertRedirects(response, reverse('home'))

    def test_whitespace_around_username_is_tolerated(self):
        response = self.client.post(reverse('login'), {
            'username': ' admin ', 'password': 'ChangeMe-Strong-Pw-93',
        })
        self.assertRedirects(response, reverse('home'))


class SeedAdminCommandTests(TestCase):
    # Regression: seed_admin used to skip existing accounts entirely, so
    # rotating DJANGO_SUPERUSER_PASSWORD in .env and restarting had no
    # effect - the account silently kept its original password forever.
    def test_creates_superuser_from_env(self):
        os.environ['DJANGO_SUPERUSER_USERNAME'] = 'newadmin'
        os.environ['DJANGO_SUPERUSER_EMAIL'] = 'newadmin@example.com'
        os.environ['DJANGO_SUPERUSER_PASSWORD'] = 'first-Password-1'
        try:
            call_command('seed_admin')
        finally:
            for key in ('DJANGO_SUPERUSER_USERNAME', 'DJANGO_SUPERUSER_EMAIL', 'DJANGO_SUPERUSER_PASSWORD'):
                os.environ.pop(key, None)

        user = User.objects.get(username='newadmin')
        self.assertTrue(user.check_password('first-Password-1'))
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)

    def test_rotated_password_is_synced_on_existing_account(self):
        User.objects.create_user('admin', password='old-Password-1')

        os.environ['DJANGO_SUPERUSER_USERNAME'] = 'admin'
        os.environ['DJANGO_SUPERUSER_EMAIL'] = 'admin@example.com'
        os.environ['DJANGO_SUPERUSER_PASSWORD'] = 'rotated-Password-2'
        try:
            call_command('seed_admin')
        finally:
            for key in ('DJANGO_SUPERUSER_USERNAME', 'DJANGO_SUPERUSER_EMAIL', 'DJANGO_SUPERUSER_PASSWORD'):
                os.environ.pop(key, None)

        user = User.objects.get(username='admin')
        self.assertFalse(user.check_password('old-Password-1'))
        self.assertTrue(user.check_password('rotated-Password-2'))
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)
