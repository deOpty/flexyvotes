"""Tests for the public site, organizer tools, tickets and legacy entry points."""
import os
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.models import AuditEvent
from core.tests.factories import PASSWORD, add_position, grant, make_event, make_org, make_user
from voting.models import Candidate, Category, Event, Profile, Ticket, TicketPurchase


class PublicSiteTests(TestCase):
    def test_home_lists_only_public_elections(self):
        make_event(title='Draft one')
        make_event(title='Open one', status=Event.Status.OPEN)
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'Open one')
        self.assertNotContains(response, 'Draft one')
        self.assertContains(self.client.get(reverse('home') + '?q=open'), 'Open one')

    def test_paid_event_page_shows_live_counts_and_vote_links(self):
        event = make_event(institutional=False, status=Event.Status.OPEN)
        _, (alice, *_rest) = add_position(event, 'Best Singer')
        response = self.client.get(reverse('event_detail', args=[event.pk]))
        self.assertContains(response, reverse('payments:pay', args=[event.pk, alice.pk]))
        self.assertContains(response, 'data-live-url')

    def test_live_counts_hidden_for_secret_ballots(self):
        event = make_event(status=Event.Status.OPEN)
        self.assertEqual(self.client.get(reverse('live_counts', args=[event.pk])).status_code, 403)

    def test_contact_form_creates_support_ticket(self):
        from core.models import SupportTicket

        response = self.client.post(reverse('contact'), {'name': 'Ama', 'email': 'ama@example.com', 'subject': 'Help',
                                                         'message': 'Cannot vote', 'category': 'voting'})
        self.assertEqual(response.status_code, 302)
        ticket = SupportTicket.objects.get()
        self.assertEqual(ticket.messages.get().body, 'Cannot vote')


class OrganizerFlowTests(TestCase):
    def setUp(self):
        self.organizer = make_user('organizer')
        Profile.objects.create(user=self.organizer, is_approved_organizer=False)

    def test_unapproved_organizer_cannot_create(self):
        self.client.login(username='organizer', password=PASSWORD)
        self.assertRedirects(self.client.get(reverse('create_event')), reverse('home'), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse('dashboard')), reverse('home'), fetch_redirect_response=False)

    def test_approval_creates_personal_org_and_event_creation_works(self):
        admin = make_user('platform', staff=True, superuser=True)
        self.client.login(username='platform', password=PASSWORD)
        profile = Profile.objects.get(user=self.organizer)
        self.client.post(reverse('console:organizers'), {'profile': profile.pk, 'decision': 'approve'})
        self.client.logout()
        self.client.login(username='organizer', password=PASSWORD)
        response = self.client.post(reverse('create_event'), {
            'title': 'Campus Awards', 'description': 'x', 'voting_mode': 'Pay to Vote', 'timezone': 'Africa/Lagos',
            'currency': 'NGN', 'start_date_date': '2030-01-01', 'start_date_time': '09:00',
            'end_date_date': '2030-01-02', 'end_date_time': '18:00', 'vote_price': '50', 'platform_fee_percentage': '1',
            'primary_color': '#800020', 'accent_color': '#FFD700'})
        event = Event.objects.get(title='Campus Awards')
        self.assertRedirects(response, reverse('elections:console_overview', args=[event.pk]))
        self.assertEqual((event.status, event.currency, event.timezone), (Event.Status.DRAFT, 'NGN', 'Africa/Lagos'))
        # The organizer cannot set the platform's commission.
        self.assertEqual(event.platform_fee_percentage, Decimal('20.00'))
        self.assertEqual(event.start_date.utcoffset().total_seconds(), 0)
        self.assertEqual(event.start_date.hour, 8)  # 09:00 Lagos = 08:00 UTC
        self.assertTrue(event.organization.is_personal)
        self.assertTrue(AuditEvent.objects.filter(event_type='ELECTION_CREATED', election_id=event.pk).exists())
        self.assertIsNotNone(admin)

    def test_negative_and_invalid_prices_rejected(self):
        org = make_org('Org', admin=self.organizer)
        event = make_event(org=org, organizer=self.organizer, institutional=False)
        self.client.login(username='organizer', password=PASSWORD)
        response = self.client.post(reverse('create_ticket', args=[event.pk]),
                                    {'name': 'VIP', 'price': '-5', 'quantity_available': '10'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Ticket.objects.exists())

    def test_organizer_tools_respect_rbac_and_lifecycle(self):
        org = make_org('Org', admin=self.organizer)
        event = make_event(org=org, organizer=self.organizer)
        outsider = make_user('outsider')
        self.client.login(username='outsider', password=PASSWORD)
        response = self.client.post(reverse('add_category', args=[event.pk]), {'name': 'Hack'})
        self.assertEqual(response.status_code, 403)
        self.client.logout()
        self.client.login(username='organizer', password=PASSWORD)
        self.client.post(reverse('add_category', args=[event.pk]), {'name': 'President', 'ballot_type': 'RANKED',
                                                                    'min_select': '1', 'max_select': '3'})
        category = Category.objects.get(name='President')
        self.assertEqual(category.ballot_type, 'RANKED')
        self.client.post(reverse('bulk_add_candidates', args=[event.pk]), {'bulk_names': 'A\nB\nC', 'bulk_category': category.pk})
        self.assertEqual(Candidate.objects.filter(category=category).count(), 3)
        Event.objects.filter(pk=event.pk).update(status=Event.Status.OPEN, ballot_frozen=True, candidates_frozen=True)
        self.client.post(reverse('add_category', args=[event.pk]), {'name': 'Late'})
        self.assertFalse(Category.objects.filter(name='Late').exists())
        self.assertIsNotNone(outsider)

    def test_edit_event_keeps_price_and_converts_timezone(self):
        org = make_org('Org', admin=self.organizer)
        event = make_event(org=org, organizer=self.organizer, institutional=False, vote_price=Decimal('2.50'),
                           timezone='Africa/Accra')
        self.client.login(username='organizer', password=PASSWORD)
        page = self.client.get(reverse('edit_event', args=[event.pk]))
        self.assertContains(page, 'value="2.50"')


class TicketTests(TestCase):
    def setUp(self):
        self.owner = make_user('owner')
        self.org = make_org('Ticket Org', admin=self.owner)
        self.event = make_event(org=self.org, organizer=self.owner, institutional=False, status=Event.Status.OPEN)
        self.ticket = Ticket.objects.create(event=self.event, name='VIP', price=Decimal('10'))
        self.purchase = TicketPurchase.objects.create(ticket=self.ticket, event=self.event, buyer_name='Ama',
                                                      buyer_email='ama@x.com', paystack_reference='TK-ABC1234567',
                                                      status='Success')

    def test_scanner_checks_in_once_and_requires_permission(self):
        url = reverse('process_scan', args=[self.event.pk])
        body = '{"text": "REF: TK-ABC1234567"}'
        make_user('intruder')
        self.client.login(username='intruder', password=PASSWORD)
        self.assertEqual(self.client.post(url, body, content_type='application/json').status_code, 403)
        self.client.logout()
        self.client.login(username='owner', password=PASSWORD)
        self.assertEqual(self.client.post(url, body, content_type='application/json').status_code, 200)
        self.assertEqual(self.client.post(url, body, content_type='application/json').status_code, 409)

    def test_guestlist_export_is_formula_safe(self):
        TicketPurchase.objects.create(ticket=self.ticket, event=self.event, buyer_name='=cmd|calc', buyer_email='x@x.com',
                                      paystack_reference='TK-ZZZ1234567', status='Success')
        self.client.login(username='owner', password=PASSWORD)
        response = self.client.get(reverse('download_guestlist', args=[self.event.pk]))
        self.assertIn("'=cmd|calc", response.content.decode())

    def test_ticket_lookup_and_retrieval(self):
        response = self.client.post(reverse('tickets'), {'action': 'verify', 'reference': 'tk-abc1234567'})
        self.assertEqual(response.context['ticket_found'], self.purchase)
        response = self.client.post(reverse('retrieve_ticket'), {'phone_or_ref': 'TK-ABC1234567'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(response['Location']).status_code, 200)

    def test_send_ticket_email_requires_csrf(self):
        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        response = strict.post(reverse('send_ticket_email'), '{}', content_type='application/json')
        self.assertEqual(response.status_code, 403)


class SeedAdminCommandTests(TestCase):
    @patch.dict(os.environ, {'DJANGO_SUPERUSER_USERNAME': 'newadmin', 'DJANGO_SUPERUSER_EMAIL': 'a@example.com',
                             'DJANGO_SUPERUSER_PASSWORD': 'Some-strong-pass-1\r\n'})
    def test_creates_and_syncs_superuser(self):
        call_command('seed_admin', verbosity=0)
        user = User.objects.get(username='newadmin')
        self.assertTrue(user.is_superuser and user.check_password('Some-strong-pass-1'))
        with patch.dict(os.environ, {'DJANGO_SUPERUSER_PASSWORD': 'Rotated-pass-2'}):
            call_command('seed_admin', verbosity=0)
        user.refresh_from_db()
        self.assertTrue(user.check_password('Rotated-pass-2'))


class LegacyRedirectTests(TestCase):
    def test_legacy_vote_endpoints_forward(self):
        event = make_event(institutional=False, status=Event.Status.OPEN)
        _, (alice, *_rest) = add_position(event, 'Best')
        response = self.client.post(reverse('initiate_vote', args=[alice.pk]), {'amount': '5'})
        self.assertTrue(response['Location'].startswith(reverse('payments:pay', args=[event.pk, alice.pk])))
        self.assertTrue(self.client.get(reverse('vote_success') + '?reference=FV-X')['Location'].startswith('/payments/callback/'))

    def test_legacy_code_management_urls_require_permission(self):
        event = make_event()
        make_user('nobody')
        self.client.login(username='nobody', password=PASSWORD)
        for name in ('generate_codes', 'clear_codes', 'upload_csv', 'toggle_voting_lock'):
            self.assertEqual(self.client.post(reverse(name, args=[event.pk])).status_code, 403, name)
        self.assertEqual(self.client.get(reverse('download_codes', args=[event.pk])).status_code, 403)

    def test_grant_role_scoping_for_officer(self):
        org = make_org('X')
        event = make_event(org=org)
        officer = make_user('officer')
        grant(officer, 'ELECTION_OFFICER', org, event=event)
        self.client.login(username='officer', password=PASSWORD)
        self.assertEqual(self.client.get(reverse('elections:console_voters', args=[event.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('elections:console_settings', args=[event.pk])).status_code, 403)
