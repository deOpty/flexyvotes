import json
import re
from datetime import timedelta
from decimal import Decimal

import responses
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.tests.factories import PASSWORD, add_position, grant, make_event, make_org, make_user
from elections.models import ApprovalRequest
from fraud.models import FraudEvent
from payments import paystack, service
from payments.models import DiscountCode, Payment, Refund, VotePackage, WebhookEvent
from voting.models import Event, Ticket, TicketPurchase, VoteTransaction

SECRET = 'sk_test_unit'
BASE = 'https://api.paystack.co'


def signed(body_dict):
    body = json.dumps(body_dict).encode()
    import hashlib
    import hmac

    return body, hmac.new(SECRET.encode(), body, hashlib.sha512).hexdigest()


def gateway_data(payment, status='success', amount=None, **extra):
    data = {'status': status, 'reference': payment.reference, 'amount': payment.amount_minor if amount is None else amount,
            'currency': payment.currency, 'channel': 'card', 'id': 123, 'fees': 39, 'paid_at': timezone.now().isoformat(),
            'authorization': {'signature': extra.pop('signature', 'SIG_A'), 'last4': '4081', 'bank': 'Test',
                              'brand': 'visa', 'country_code': extra.pop('country', 'GH')}}
    data.update(extra)
    return data


@override_settings(PAYSTACK_SECRET_KEY=SECRET, PAYMENTS_FAKE_GATEWAY=False, SITE_URL='https://vote.example.com')
class PaymentServiceTests(TestCase):
    def setUp(self):
        self.owner = make_user('owner')
        self.org = make_org('Show Co', admin=self.owner)
        self.event = make_event(org=self.org, organizer=self.owner, institutional=False, status=Event.Status.OPEN)
        self.category, (self.alice, self.bob, _) = add_position(self.event, 'Best Singer')

    def mock_init(self):
        responses.add(responses.POST, f'{BASE}/transaction/initialize', json={
            'status': True, 'data': {'authorization_url': 'https://checkout.paystack.com/abc', 'access_code': 'abc'}})

    def initiate(self, **kwargs):
        from django.test import RequestFactory

        request = RequestFactory().post('/', HTTP_USER_AGENT='Mozilla/5.0 Test')
        params = {'votes': 5, 'email': 'fan@gmail.com'}
        params.update(kwargs)
        return service.initiate_vote_payment(request, self.event, kwargs.pop('candidate', self.alice), **params)

    def test_quote_with_package_discount_and_campaign_price(self):
        package = VotePackage.objects.create(event=self.event, name='Fan pack', votes=10, bonus_votes=2, price=Decimal('9'))
        DiscountCode.objects.create(event=self.event, code='HALF', kind='PERCENT', value=50)
        q = service.quote(self.event, self.alice, package=package, discount_code='half')
        self.assertEqual((q.total_votes, q.amount, q.discount), (12, Decimal('4.50'), Decimal('4.50')))
        self.category.vote_price = Decimal('2.50')
        self.category.save()
        self.alice.refresh_from_db()
        self.assertEqual(service.quote(self.event, self.alice, votes=4).amount, Decimal('10.00'))
        self.assertTrue(service.quote(self.event, self.alice, votes=0).errors)
        self.assertTrue(service.quote(self.event, self.alice, votes=1, discount_code='NOPE').errors)

    @responses.activate
    def test_vote_limits_per_voter_are_enforced(self):
        self.mock_init()
        Event.objects.filter(pk=self.event.pk).update(max_votes_per_voter=6)
        self.event.refresh_from_db()
        payment = self.initiate(votes=5)
        service.apply_gateway_result(payment, gateway_data(payment), 'TEST')
        with self.assertRaisesMessage(service.PaymentError, 'at most 6 votes'):
            self.initiate(votes=2)

    @responses.activate
    def test_verified_payment_credits_votes_exactly_once(self):
        self.mock_init()
        payment = self.initiate()
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(payment.authorization_url, 'https://checkout.paystack.com/abc')
        sent = json.loads(responses.calls[0].request.body)
        self.assertEqual(sent['amount'], 500)
        self.assertEqual(VoteTransaction.objects.count(), 0)  # nothing until verified
        for _ in range(3):
            service.apply_gateway_result(payment, gateway_data(payment), 'WEBHOOK')
        payment.refresh_from_db()
        self.assertTrue(payment.votes_credited)
        ledger = VoteTransaction.objects.get()
        self.assertEqual((ledger.number_of_votes, ledger.payment_id), (5, payment.pk))
        self.assertEqual(payment.card_last4, '4081')
        self.assertTrue(payment.history.filter(to_status='SUCCESS').exists())

    @responses.activate
    def test_amount_mismatch_is_held_not_credited(self):
        self.mock_init()
        payment = self.initiate()
        service.apply_gateway_result(payment, gateway_data(payment, amount=100), 'WEBHOOK')
        payment.refresh_from_db()
        self.assertTrue(payment.held)
        self.assertFalse(payment.votes_credited)
        self.assertTrue(FraudEvent.objects.filter(payment=payment, decision='HOLD').exists())

    @responses.activate
    def test_idempotency_key_returns_the_same_payment(self):
        self.mock_init()
        first = self.initiate(idempotency_key='key-123456')
        second = self.initiate(idempotency_key='key-123456')
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_failed_and_abandoned_payments(self):
        self.mock_init()
        failed = self.initiate()
        service.apply_gateway_result(failed, gateway_data(failed, status='failed'), 'WEBHOOK')
        failed.refresh_from_db()
        self.assertEqual(failed.status, Payment.Status.FAILED)
        stale = self.initiate()
        Payment.objects.filter(pk=stale.pk).update(created_at=timezone.now() - timedelta(hours=2))
        responses.add(responses.GET, f'{BASE}/transaction/verify/{stale.reference}',
                      json={'status': True, 'data': {'status': 'abandoned', 'reference': stale.reference}})
        service.expire_abandoned()
        stale.refresh_from_db()
        self.assertEqual(stale.status, Payment.Status.ABANDONED)

    @responses.activate
    def test_receipt_is_a_bearer_link_without_payer_details(self):
        """Receipts are reachable by their unguessable reference only, and
        never disclose who paid."""
        self.mock_init()
        payment = self.initiate(email='private.fan@gmail.com', phone='0241234567')
        self.assertRegex(payment.reference, r'^FV-[A-Z0-9]{14}$')
        page = self.client.get(reverse('payments:receipt', args=[payment.reference]))
        self.assertEqual(page.status_code, 200)
        api = self.client.get(f'/api/v1/payments/{payment.reference}')
        self.assertEqual(api.status_code, 200)
        for body in (page.content.decode(), api.content.decode()):
            self.assertNotIn('private.fan', body)
            self.assertNotIn('0241234567', body)
        self.assertEqual(self.client.get(reverse('payments:receipt', args=['FV-DOESNOTEXIST00'])).status_code, 404)

    @responses.activate
    def test_callback_verifies_with_gateway(self):
        self.mock_init()
        payment = self.initiate()
        responses.add(responses.GET, f'{BASE}/transaction/verify/{payment.reference}',
                      json={'status': True, 'data': gateway_data(payment)})
        response = self.client.get(reverse('payments:callback') + f'?reference={payment.reference}')
        self.assertRedirects(response, reverse('payments:receipt', args=[payment.reference]))
        payment.refresh_from_db()
        self.assertTrue(payment.votes_credited)
        self.assertContains(self.client.get(reverse('payments:receipt', args=[payment.reference])), 'Votes credited')

    @responses.activate
    def test_webhook_signature_and_replay_protection(self):
        self.mock_init()
        payment = self.initiate()
        body, signature = signed({'event': 'charge.success', 'data': gateway_data(payment)})
        url = reverse('paystack_webhook')
        self.assertEqual(self.client.post(url, body, content_type='application/json',
                                          HTTP_X_PAYSTACK_SIGNATURE='bad').status_code, 401)
        self.assertEqual(self.client.post(url, body, content_type='application/json',
                                          HTTP_X_PAYSTACK_SIGNATURE=signature).status_code, 200)
        replay = self.client.post(reverse('payments:webhook'), body, content_type='application/json',
                                  HTTP_X_PAYSTACK_SIGNATURE=signature)
        self.assertEqual(replay.content, b'duplicate')
        self.assertEqual(VoteTransaction.objects.count(), 1)
        self.assertEqual(WebhookEvent.objects.get().attempts, 2)

    @responses.activate
    def test_refund_below_threshold_reverses_votes(self):
        self.mock_init()
        payment = self.initiate()
        service.apply_gateway_result(payment, gateway_data(payment), 'WEBHOOK')
        responses.add(responses.POST, f'{BASE}/refund', json={'status': True, 'data': {'id': 77, 'status': 'pending'}})
        refund = service.request_refund(payment, payment.amount, 'Customer request', self.owner)
        self.assertEqual(refund.status, Refund.Status.PROCESSING)
        body, signature = signed({'event': 'refund.processed', 'data': {'transaction': {'reference': payment.reference}}})
        self.client.post(reverse('paystack_webhook'), body, content_type='application/json',
                         HTTP_X_PAYSTACK_SIGNATURE=signature)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.REFUNDED)
        self.assertEqual(VoteTransaction.objects.get().status, VoteTransaction.Status.REVERSED)

    @responses.activate
    @override_settings(REFUND_DUAL_APPROVAL_THRESHOLD='1')
    def test_large_refund_needs_a_second_approver(self):
        self.mock_init()
        Event.objects.filter(pk=self.event.pk).update(dual_approval_required=True)
        self.event.refresh_from_db()
        payment = self.initiate()
        service.apply_gateway_result(payment, gateway_data(payment), 'WEBHOOK')
        refund = service.request_refund(payment, payment.amount, 'Duplicate charge', self.owner)
        self.assertEqual(refund.status, Refund.Status.REQUESTED)
        request = ApprovalRequest.objects.get(action='REFUND')
        from elections import integrity

        with self.assertRaises(integrity.IntegrityControlError):
            integrity.decide(request, self.owner, True)
        approver = make_user('finance2')
        grant(approver, 'ORG_ADMIN', self.org)
        responses.add(responses.POST, f'{BASE}/refund', json={'status': True, 'data': {'id': 78}})
        integrity.decide(request, approver, True, 'ok')
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.PROCESSING)

    @responses.activate
    def test_chargeback_reverses_votes_and_flags(self):
        self.mock_init()
        payment = self.initiate()
        service.apply_gateway_result(payment, gateway_data(payment), 'WEBHOOK')
        body, signature = signed({'event': 'charge.dispute.create', 'data': {'transaction': {'reference': payment.reference}}})
        self.client.post(reverse('paystack_webhook'), body, content_type='application/json',
                         HTTP_X_PAYSTACK_SIGNATURE=signature)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.DISPUTED)
        self.assertEqual(VoteTransaction.objects.get().status, VoteTransaction.Status.REVERSED)
        self.assertTrue(FraudEvent.objects.filter(kind='CHARGEBACK').exists())

    @responses.activate
    def test_reconciliation_recovers_missed_webhook(self):
        self.mock_init()
        payment = self.initiate()
        Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(minutes=20))
        responses.add(responses.GET, f'{BASE}/transaction', json={'status': True, 'data': [
            gateway_data(payment), {'reference': 'FV-UNKNOWN', 'status': 'success', 'amount': 1000}],
            'meta': {'pageCount': 1}})
        run = service.reconcile(timezone.now() - timedelta(hours=1), timezone.now())
        payment.refresh_from_db()
        self.assertTrue(payment.votes_credited)
        kinds = set(run.items.values_list('kind', flat=True))
        self.assertIn('STATUS_MISMATCH', kinds)
        self.assertIn('MISSING_LOCALLY', kinds)
        self.assertEqual(run.status, 'COMPLETED')

    @responses.activate
    def test_card_vote_limit_holds_payment_until_review(self):
        self.mock_init()
        Event.objects.filter(pk=self.event.pk).update(max_votes_per_voter=6)
        self.event.refresh_from_db()
        first = self.initiate(votes=5, email='one@gmail.com')
        service.apply_gateway_result(first, gateway_data(first, signature='SHARED'), 'WEBHOOK')
        # A different email, but the same card: the card is now over the limit.
        second = self.initiate(votes=5, email='two@gmail.com')
        service.apply_gateway_result(second, gateway_data(second, signature='SHARED'), 'WEBHOOK')
        second.refresh_from_db()
        self.assertTrue(second.held)
        self.assertFalse(second.votes_credited)
        self.assertEqual(VoteTransaction.objects.count(), 1)
        alert = FraudEvent.objects.filter(payment=second, decision='HOLD').first()
        self.assertIsNotNone(alert)
        self.assertIn('card_vote_limit', [sig['code'] for sig in alert.signals])
        from fraud.service import review

        review(alert, self.owner, 'dismiss', 'Family members sharing a card')
        second.refresh_from_db()
        self.assertTrue(second.votes_credited)
        self.assertEqual(VoteTransaction.objects.count(), 2)

    @responses.activate
    def test_disposable_email_and_bot_user_agent_raise_risk(self):
        self.mock_init()
        from django.test import RequestFactory

        request = RequestFactory().post('/', HTTP_USER_AGENT='python-requests/2.31')
        payment = service.initiate_vote_payment(request, self.event, self.alice, votes=1, email='x@mailinator.com')
        self.assertGreaterEqual(payment.risk_score, 61)
        alert = FraudEvent.objects.get(payment=payment)
        codes = {sig['code'] for sig in alert.signals}
        self.assertTrue({'disposable_email', 'automation_user_agent'} <= codes)

    def test_paid_vote_page_and_quote_endpoint(self):
        url = reverse('payments:pay', args=[self.event.pk, self.alice.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.get(reverse('payments:quote', args=[self.event.pk, self.alice.pk]) + '?votes=3')
        self.assertEqual(response.json()['amount'], '3.00')


@override_settings(PAYSTACK_SECRET_KEY=None, PAYMENTS_FAKE_GATEWAY=True, DEBUG=True)
class SimulatorFlowTests(TestCase):
    def test_full_pay_webhook_verify_credit_flow_with_simulator(self):
        owner = make_user('owner')
        org = make_org('Show', admin=owner)
        event = make_event(org=org, institutional=False, status=Event.Status.OPEN)
        _, (alice, *_rest) = add_position(event, 'Best Dancer')
        response = self.client.post(reverse('payments:pay', args=[event.pk, alice.pk]),
                                    {'votes': '3', 'email': 'fan@gmail.com', 'idempotency_key': 'abcdefgh1234'})
        payment = Payment.objects.get()
        self.assertTrue(response['Location'].endswith(reverse('payments:simulator', args=[payment.reference])))
        self.assertEqual(self.client.get(reverse('payments:simulator', args=[payment.reference])).status_code, 200)
        response = self.client.post(reverse('payments:simulator', args=[payment.reference]), {'outcome': 'pay'})
        payment.refresh_from_db()
        self.assertTrue(payment.votes_credited)
        self.assertEqual(VoteTransaction.objects.get().number_of_votes, 3)
        # Repeating the form submission (same idempotency key) does not create a new payment.
        self.client.post(reverse('payments:pay', args=[event.pk, alice.pk]),
                         {'votes': '3', 'email': 'fan@gmail.com', 'idempotency_key': 'abcdefgh1234'})
        self.assertEqual(Payment.objects.count(), 1)
        live = self.client.get(reverse('live_counts', args=[event.pk])).json()
        self.assertEqual(live['total_votes'], 3)


@override_settings(PAYSTACK_SECRET_KEY=SECRET, PAYMENTS_FAKE_GATEWAY=False, USSD_CALLBACK_TOKEN='ussd-secret')
class UssdAndTicketTests(TestCase):
    def setUp(self):
        self.event = make_event(institutional=False, status=Event.Status.OPEN)
        _, (self.alice, *_rest) = add_position(self.event, 'Best Rapper')

    def ussd(self, text, token='ussd-secret', session='s1'):
        return self.client.post(reverse('ussd_callback') + f'?token={token}',
                                {'sessionId': session, 'phoneNumber': '+233241234567', 'text': text})

    def test_ussd_requires_token(self):
        self.assertEqual(self.ussd('', token='nope').status_code, 403)
        self.assertTrue(self.ussd('').content.startswith(b'CON Welcome'))

    @responses.activate
    def test_ussd_vote_creates_pending_mobile_money_charge(self):
        responses.add(responses.POST, f'{BASE}/charge', json={'status': True, 'data': {'status': 'pay_offline'}})
        response = self.ussd(f'1*{self.alice.nominee_code}*4*1')
        self.assertIn(b'Approve', response.content)
        payment = Payment.objects.get()
        self.assertEqual((payment.status, payment.votes_credited, payment.channel), (Payment.Status.PENDING, False, 'mobile_money'))
        charge = json.loads(responses.calls[0].request.body)
        self.assertEqual(charge['mobile_money'], {'phone': '0241234567', 'provider': 'mtn'})
        self.assertEqual(VoteTransaction.objects.count(), 0)

    @responses.activate
    def test_ticket_purchase_verified_by_webhook_with_amount_check(self):
        ticket = Ticket.objects.create(event=self.event, name='VIP', price=Decimal('50'), quantity_available=2)
        responses.add(responses.POST, f'{BASE}/transaction/initialize', json={
            'status': True, 'data': {'authorization_url': 'https://checkout.paystack.com/t', 'access_code': 't'}})
        response = self.client.post(reverse('buy_ticket', args=[ticket.pk]), {'name': 'Ama', 'email': 'ama@gmail.com', 'quantity': 2})
        self.assertEqual(response.status_code, 302)
        purchase = TicketPurchase.objects.get()
        self.assertTrue(re.match(r'^TK-[A-Z0-9]{10}$', purchase.paystack_reference))
        # Overselling is blocked while the first purchase is pending.
        self.client.post(reverse('buy_ticket', args=[ticket.pk]), {'name': 'Kojo', 'email': 'kojo@gmail.com', 'quantity': 1})
        self.assertEqual(TicketPurchase.objects.count(), 1)
        body, signature = signed({'event': 'charge.success', 'data': {
            'reference': purchase.paystack_reference, 'status': 'success', 'amount': 5000, 'metadata': {'type': 'ticket_purchase'}}})
        self.client.post(reverse('paystack_webhook'), body, content_type='application/json', HTTP_X_PAYSTACK_SIGNATURE=signature)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, 'Pending')  # underpaid -> not issued
        body, signature = signed({'event': 'charge.success', 'data': {
            'reference': purchase.paystack_reference, 'status': 'success', 'amount': 10000, 'metadata': {'type': 'ticket_purchase'}}})
        self.client.post(reverse('paystack_webhook'), body, content_type='application/json', HTTP_X_PAYSTACK_SIGNATURE=signature)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, 'Success')

    def test_ticket_tie_breaker_vote(self):
        Event.objects.filter(pk=self.event.pk).update(enable_tie_breaker=True)
        ticket = Ticket.objects.create(event=self.event, name='Regular', price=Decimal('10'))
        TicketPurchase.objects.create(ticket=ticket, event=self.event, buyer_email='x@y.com', quantity=2,
                                      paystack_reference='TK-ABCDEFGH12', status='Success')
        url = reverse('cast_vote_with_code', args=[self.alice.pk])
        self.client.post(url, {'code': 'TK-ABCDEFGH12'})
        self.client.post(url, {'code': 'TK-ABCDEFGH12'})
        ledger = VoteTransaction.objects.get()
        self.assertEqual((ledger.vote_type, ledger.number_of_votes), ('Tie-Breaker', 2))


class PaystackHelpersTests(TestCase):
    def test_ghana_momo_provider_detection(self):
        self.assertEqual(paystack.ghana_momo_provider('+233 24 123 4567')[0], 'mtn')
        self.assertEqual(paystack.ghana_momo_provider('0201234567')[0], 'vod')
        self.assertEqual(paystack.ghana_momo_provider('0271234567')[0], 'atl')
        self.assertIsNone(paystack.ghana_momo_provider('0301234567')[0])

    def test_finance_console_requires_permission(self):
        make_user('random')
        self.client.login(username='random', password=PASSWORD)
        self.assertEqual(self.client.get(reverse('payments:reconciliation')).status_code, 403)


class ReconciliationScopingTests(TestCase):
    """Reconciliation runs are platform-wide, but each tenant may only see and
    resolve discrepancies on its own payments."""

    def setUp(self):
        from payments.models import ReconciliationItem, ReconciliationRun

        self.admin_a, self.admin_b = make_user('recon_admin_a'), make_user('recon_admin_b')
        self.org_a = make_org('Show A', admin=self.admin_a)
        self.org_b = make_org('Show B', admin=self.admin_b)
        self.finance_a, self.auditor_a = make_user('finance_a'), make_user('auditor_a')
        grant(self.finance_a, 'FINANCE_OFFICER', self.org_a)
        grant(self.auditor_a, 'ELECTION_AUDITOR', self.org_a)
        run = ReconciliationRun.objects.create(window_start=timezone.now() - timedelta(hours=1), window_end=timezone.now())
        self.items = {}
        for label, org in (('a', self.org_a), ('b', self.org_b)):
            event = make_event(org=org, institutional=False, status=Event.Status.OPEN)
            payment = Payment.objects.create(reference=Payment.new_reference(), event=event, amount=Decimal('10'),
                                             gross_amount=Decimal('10'), unit_price=Decimal('1'),
                                             currency='GHS', votes=10, payer_email=f'{label}@example.com')
            self.items[label] = ReconciliationItem.objects.create(
                run=run, payment=payment, reference=payment.reference, kind=ReconciliationItem.Kind.NOT_CREDITED)

    def page(self, user):
        self.client.force_login(user)
        return self.client.get(reverse('payments:reconciliation'))

    def resolve(self, item):
        return self.client.post(reverse('payments:reconciliation'),
                                {'action': 'resolve', 'item': item.pk, 'note': 'checked'})

    def test_finance_officer_sees_and_resolves_only_own_tenant(self):
        page = self.page(self.finance_a)
        self.assertContains(page, self.items['a'].reference)
        self.assertNotContains(page, self.items['b'].reference)
        self.assertNotContains(page, 'Run now')
        self.assertEqual(self.resolve(self.items['b']).status_code, 404)
        self.assertEqual(self.client.post(reverse('payments:reconciliation'), {'action': 'run'}).status_code, 403)
        self.assertRedirects(self.resolve(self.items['a']), reverse('payments:reconciliation'))
        self.items['a'].refresh_from_db()
        self.assertEqual(self.items['a'].resolution, 'RESOLVED')

    def test_auditor_can_inspect_but_not_resolve(self):
        page = self.page(self.auditor_a)
        self.assertContains(page, self.items['a'].reference)
        self.assertNotContains(page, self.items['b'].reference)
        self.assertNotContains(page, 'name="action" value="resolve"')
        self.assertEqual(self.resolve(self.items['a']).status_code, 403)

    def test_platform_admin_sees_everything(self):
        staff = make_user('recon_staff', staff=True)
        page = self.page(staff)
        self.assertContains(page, self.items['a'].reference)
        self.assertContains(page, self.items['b'].reference)
        self.assertContains(page, 'Run now')
