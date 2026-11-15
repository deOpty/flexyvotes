"""The console menu (sidebar) and an election's tabs stay on every staff page,
so admins and organizers never need the browser's back button."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from core.tests.factories import PASSWORD, add_position, make_event, make_org, make_user
from core.tests.test_organizer_access import approved_organizer
from voting.models import Candidate, Category, Product, ProductCategory, Ticket

SIDEBAR = 'id="console-nav"'
TABS = 'election-tabs'


class ConsoleShellMixin:
    def assertConsolePage(self, url, tabs=False, status=200):
        response = self.client.get(url)
        self.assertEqual(response.status_code, status, url)
        html = response.content.decode()
        self.assertIn(SIDEBAR, html, f'console menu missing on {url}')
        self.assertIn('data-bs-target="#console-nav"', html, f'phone menu button missing on {url}')
        if tabs:
            self.assertIn(TABS, html, f'election tabs missing on {url}')
        return html

    def assertActiveTab(self, html, href):
        self.assertIn(f'class="nav-link active" aria-current="page" href="{href}"', html)


class AdminPagesKeepMenuTests(ConsoleShellMixin, TestCase):
    def setUp(self):
        self.admin = make_user('platform', staff=True, superuser=True)
        org = make_org('Awards Org')
        self.paid = make_event(org=org, organizer=self.admin, institutional=False,
                               start_offset=timedelta(days=1), end_offset=timedelta(days=2))
        self.institutional = make_event(org=org, organizer=self.admin, start_offset=timedelta(days=1),
                                        end_offset=timedelta(days=2))
        self.category, (self.candidate, *_) = add_position(self.paid, 'Best Artist')
        self.ticket = Ticket.objects.create(event=self.paid, name='VIP', price=Decimal('50'))
        self.product = Product.objects.create(name='Plaque', price=Decimal('10'),
                                              category=ProductCategory.objects.create(name='Awards'))
        self.client.login(username='platform', password=PASSWORD)

    def test_every_election_page_shows_menu_and_tabs(self):
        p, i = self.paid.pk, self.institutional.pk
        payments = reverse('payments:console_list') + f'?event={p}'
        pages = {  # page -> the tab that should be marked current
            reverse('edit_event', args=[p]): reverse('edit_event', args=[p]),
            reverse('edit_category', args=[self.category.pk]): reverse('elections:console_ballot', args=[p]),
            reverse('edit_candidate', args=[self.candidate.pk]): reverse('elections:console_ballot', args=[p]),
            reverse('event_analytics', args=[p]): reverse('event_analytics', args=[p]),
            reverse('event_guestlist', args=[p]): reverse('event_guestlist', args=[p]),
            reverse('event_scanner', args=[p]): reverse('event_guestlist', args=[p]),
            reverse('create_ticket', args=[p]): reverse('event_guestlist', args=[p]),
            reverse('edit_ticket', args=[self.ticket.pk]): reverse('event_guestlist', args=[p]),
            reverse('payments:pricing', args=[p]): reverse('payments:pricing', args=[p]),
            payments: payments,
            reverse('elections:console_overview', args=[p]): reverse('elections:console_overview', args=[p]),
            reverse('elections:console_ballot', args=[p]): reverse('elections:console_ballot', args=[p]),
            reverse('elections:console_results', args=[p]): reverse('elections:console_results', args=[p]),
            reverse('elections:console_schedule', args=[p]): reverse('elections:console_schedule', args=[p]),
            reverse('elections:console_voters', args=[i]): reverse('elections:console_voters', args=[i]),
            reverse('elections:console_eligibility', args=[i]): reverse('elections:console_eligibility', args=[i]),
        }
        for url, tab_href in pages.items():
            with self.subTest(url=url):
                html = self.assertConsolePage(url, tabs=True)
                self.assertActiveTab(html, tab_href)
                # The sidebar highlights "Elections" on every page about one election, except an
                # election's payments, which is the Payments page filtered (and highlights Payments).
                section = 'payments:console_list' if url == payments else 'elections:console_list'
                self.assertIn(f'class="nav-link active" href="{reverse(section)}"', html)

    def test_admin_tabs_include_details_tickets_pricing_payments_and_analytics(self):
        html = self.assertConsolePage(reverse('elections:console_overview', args=[self.paid.pk]), tabs=True)
        for name in ('edit_event', 'event_analytics', 'event_guestlist', 'payments:pricing'):
            self.assertIn(reverse(name, args=[self.paid.pk]), html)
        self.assertIn(f'{reverse("payments:console_list")}?event={self.paid.pk}', html)

    def test_pages_without_an_election_keep_the_menu(self):
        for url in (reverse('dashboard'), reverse('elections:console_list'), reverse('create_event'),
                    reverse('manage_store'), reverse('add_product'), reverse('edit_product', args=[self.product.pk]),
                    reverse('account:security'), reverse('password_change'), reverse('payments:console_list'),
                    reverse('console:access_requests'), reverse('console:notifications')):
            with self.subTest(url=url):
                self.assertConsolePage(url)

    def test_store_and_blocklist_highlight_their_own_menu_item(self):
        html = self.assertConsolePage(reverse('add_product'))
        self.assertIn(f'class="nav-link active" href="{reverse("manage_store")}"', html)
        html = self.assertConsolePage(reverse('fraud:blocklist'))
        self.assertIn(f'class="nav-link active" href="{reverse("fraud:blocklist")}"', html)
        self.assertNotIn(f'class="nav-link active" href="{reverse("fraud:alerts")}"', html)

    def test_ballot_forms_return_to_the_console_ballot_tab(self):
        ballot = reverse('elections:console_ballot', args=[self.paid.pk])
        response = self.client.post(reverse('add_category', args=[self.paid.pk]), {'name': 'Best Group'})
        self.assertRedirects(response, ballot)
        response = self.client.post(reverse('add_candidate', args=[self.paid.pk]),
                                    {'name': 'Kofi', 'category': self.category.pk})
        self.assertRedirects(response, ballot)
        response = self.client.post(reverse('bulk_add_candidates', args=[self.paid.pk]),
                                    {'bulk_names': 'Esi\nYaw', 'bulk_category': self.category.pk})
        self.assertRedirects(response, ballot)
        self.assertTrue(Category.objects.filter(name='Best Group').exists())
        self.assertEqual(Candidate.objects.filter(name__in=['Kofi', 'Esi', 'Yaw']).count(), 3)
        # Errors also come back to the Ballot tab, with the message shown there.
        response = self.client.post(reverse('add_category', args=[self.paid.pk]), {'name': ''}, follow=True)
        self.assertRedirects(response, ballot)
        self.assertContains(response, 'Position name is required.')
        # The JSON answer for scripted callers is unchanged.
        response = self.client.post(reverse('add_category', args=[self.paid.pk]), {'name': 'Ajax'},
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.json()['status'], 'success')

    def test_ticket_forms_return_to_the_tickets_tab(self):
        tickets = reverse('event_guestlist', args=[self.paid.pk])
        response = self.client.post(reverse('create_ticket', args=[self.paid.pk]),
                                    {'name': 'Regular', 'price': '20', 'quantity_available': '100'})
        self.assertRedirects(response, tickets)
        response = self.client.post(reverse('edit_ticket', args=[self.ticket.pk]),
                                    {'name': 'VIP', 'price': '60', 'quantity_available': '50', 'is_active': 'on'})
        self.assertRedirects(response, tickets)
        # Ticket types can be edited from the Tickets tab.
        self.assertContains(self.client.get(tickets), reverse('edit_ticket', args=[self.ticket.pk]))

    def test_ballot_preview_links_back_to_the_console(self):
        response = self.client.get(reverse('elections:console_ballot_preview', args=[self.institutional.pk]))
        self.assertContains(response, reverse('elections:console_ballot', args=[self.institutional.pk]))

    def test_payments_list_ignores_a_bad_election_filter(self):
        self.assertConsolePage(reverse('payments:console_list') + '?event=abc')

    def test_not_found_page_keeps_the_menu_for_console_users(self):
        html = self.assertConsolePage('/console/no-such-page/', status=404)
        self.assertIn('Back to dashboard', html)

    def test_admin_sees_money_on_analytics(self):
        self.assertContains(self.client.get(reverse('event_analytics', args=[self.paid.pk])), 'Organizer payout')


class OrganizerPagesKeepMenuTests(ConsoleShellMixin, TestCase):
    def setUp(self):
        self.organizer, self.org = approved_organizer()
        self.event = make_event(org=self.org, organizer=self.organizer, institutional=False,
                                start_offset=timedelta(days=1), end_offset=timedelta(days=2))
        self.category, (self.candidate, *_) = add_position(self.event, 'Best Artist')
        self.client.login(username='organizer', password=PASSWORD)

    def test_organizer_pages_show_menu_and_tabs(self):
        e = self.event.pk
        for url in (reverse('edit_event', args=[e]), reverse('edit_category', args=[self.category.pk]),
                    reverse('edit_candidate', args=[self.candidate.pk]), reverse('event_analytics', args=[e]),
                    reverse('elections:console_overview', args=[e]), reverse('elections:console_ballot', args=[e]),
                    reverse('elections:console_results', args=[e]), reverse('elections:console_monitor', args=[e])):
            with self.subTest(url=url):
                self.assertConsolePage(url, tabs=True)
        for url in (reverse('dashboard'), reverse('elections:console_list'), reverse('create_event'),
                    reverse('account:security'), reverse('password_change'), reverse('console:access_requests')):
            with self.subTest(url=url):
                self.assertConsolePage(url)

    def test_organizer_tabs_hide_admin_sections(self):
        html = self.assertConsolePage(reverse('edit_event', args=[self.event.pk]), tabs=True)
        self.assertIn(reverse('edit_event', args=[self.event.pk]), html)
        for name in ('event_guestlist', 'payments:pricing', 'elections:console_settings', 'elections:console_schedule',
                     'elections:console_audit'):
            self.assertNotIn(reverse(name, args=[self.event.pk]), html)
        self.assertNotIn(f'{reverse("payments:console_list")}?event=', html)

    def test_analytics_hides_money_from_organizers(self):
        response = self.client.get(reverse('event_analytics', args=[self.event.pk]))
        self.assertNotContains(response, 'Organizer payout')
        self.assertNotContains(response, 'Total revenue')

    def test_forbidden_page_keeps_the_menu_and_offers_an_access_request(self):
        html = self.assertConsolePage(reverse('elections:console_settings', args=[self.event.pk]), status=403)
        self.assertIn(reverse('console:access_requests'), html)
        self.assertIn('Back to dashboard', html)


class PublicUsersKeepPublicLayoutTests(TestCase):
    def test_account_and_error_pages_use_the_public_layout_for_non_console_users(self):
        make_user('voter')
        self.client.login(username='voter', password=PASSWORD)
        for url in (reverse('account:security'), reverse('password_change')):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, SIDEBAR)
        response = self.client.get('/no-such-page/')
        self.assertNotContains(response, SIDEBAR, status_code=404)
        self.assertContains(response, 'Back to home', status_code=404)

    def test_anonymous_not_found_page_is_public(self):
        response = self.client.get('/no-such-page/')
        self.assertNotContains(response, SIDEBAR, status_code=404)
