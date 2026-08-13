import hashlib
import hmac
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Candidate, Event, Ticket, TicketPurchase, VoteTransaction, VotingCode


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
