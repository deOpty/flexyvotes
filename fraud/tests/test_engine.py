from django.test import RequestFactory, TestCase, override_settings

from core.tests.factories import PASSWORD, make_event, make_user
from fraud import engine, service
from fraud.models import BlocklistEntry, FraudEvent


class RiskEngineTests(TestCase):
    def request(self, ua='Mozilla/5.0', ip='41.66.1.1', **meta):
        return RequestFactory().post('/', HTTP_USER_AGENT=ua, REMOTE_ADDR=ip, **meta)

    def test_clean_request_is_allowed_and_not_recorded(self):
        result = engine.assess(FraudEvent.Kind.PAYMENT, request=self.request(), email='ama@gmail.com')
        self.assertEqual((result.score, result.decision), (0, 'ALLOW'))
        self.assertFalse(FraudEvent.objects.exists())

    def test_thresholds(self):
        self.assertEqual(engine.decide(30), 'ALLOW')
        self.assertEqual(engine.decide(31), 'MONITOR')
        self.assertEqual(engine.decide(61), 'CHALLENGE')
        self.assertEqual(engine.decide(81), 'HOLD')

    def test_velocity_from_one_ip(self):
        for _ in range(31):
            result = engine.assess(FraudEvent.Kind.PAYMENT, request=self.request(), email='a@gmail.com', record=False)
        self.assertIn('ip_velocity_high', [s['code'] for s in result.signals])

    def test_blocklists_and_anonymizers(self):
        service.add_block(BlocklistEntry.Kind.EMAIL, 'evil@gmail.com', None, 'chargebacks')
        service.add_block(BlocklistEntry.Kind.ANONYMIZER, '185.220.100.0/22', None, 'Tor exits')
        result = engine.assess(FraudEvent.Kind.PAYMENT, request=self.request(ip='185.220.101.5'), email='Evil@gmail.com')
        codes = {s['code'] for s in result.signals}
        self.assertTrue({'blocklisted', 'anonymizer'} <= codes)
        self.assertEqual(result.decision, 'HOLD')
        self.assertNotIn('evil@gmail.com', BlocklistEntry.objects.get(kind='EMAIL').value)

    @override_settings(FRAUD_FLAG_PROXIES=False)
    def test_proxy_flagging_can_be_disabled(self):
        service.add_block(BlocklistEntry.Kind.ANONYMIZER, '185.220.100.0/22', None)
        result = engine.assess(FraudEvent.Kind.PAYMENT, request=self.request(ip='185.220.101.5'), record=False)
        self.assertNotIn('anonymizer', [s['code'] for s in result.signals])

    def test_unexpected_country(self):
        event = make_event(institutional=False, allowed_countries=['GH'])
        result = engine.assess(FraudEvent.Kind.PAYMENT, event=event, card_country='RU', record=False, count_velocity=False)
        self.assertIn('unexpected_country', [s['code'] for s in result.signals])

    def test_fraud_console(self):
        staff = make_user('analyst', staff=True)
        alert = FraudEvent.objects.create(kind='PAYMENT', decision='MONITOR', score=40, signals=[{'code': 'x', 'weight': 40}])
        self.client.login(username='analyst', password=PASSWORD)
        self.assertContains(self.client.get('/console/fraud/'), 'MONITOR'.title()[:3])
        self.client.post(f'/console/fraud/{alert.pk}/', {'outcome': 'confirm', 'notes': 'bot farm'})
        alert.refresh_from_db()
        self.assertEqual((alert.status, alert.reviewed_by), ('CONFIRMED', staff))
        self.assertEqual(self.client.get('/console/fraud/blocklist/').status_code, 200)


class BlocklistTenancyTests(TestCase):
    """One tenant's blocklist must never affect, or be visible to, another."""

    def setUp(self):
        from core.tests.factories import grant, make_org

        self.org_a = make_org('Show A', admin=make_user('bl_admin_a'))
        self.org_b = make_org('Show B', admin=make_user('bl_admin_b'))
        self.event_a = make_event(org=self.org_a, institutional=False)
        self.event_b = make_event(org=self.org_b, institutional=False)
        self.analyst_a, self.analyst_b = make_user('analyst_a'), make_user('analyst_b')
        grant(self.analyst_a, 'FRAUD_ANALYST', self.org_a)
        grant(self.analyst_b, 'FRAUD_ANALYST', self.org_b)

    def score(self, event, ip):
        return engine.assess(FraudEvent.Kind.PAYMENT, event=event, ip=ip, record=False, count_velocity=False)

    def test_organization_entry_only_applies_to_that_organization(self):
        service.add_block(BlocklistEntry.Kind.IP, '102.176.1.9', self.analyst_a, 'farm', organization=self.org_a)
        self.assertIn('blocklisted', [s['code'] for s in self.score(self.event_a, '102.176.1.9').signals])
        self.assertNotIn('blocklisted', [s['code'] for s in self.score(self.event_b, '102.176.1.9').signals])
        service.add_block(BlocklistEntry.Kind.IP, '102.176.1.10', None, 'platform')  # platform-wide
        for event in (self.event_a, self.event_b):
            self.assertIn('blocklisted', [s['code'] for s in self.score(event, '102.176.1.10').signals])

    def test_analysts_only_see_and_manage_their_own_entries(self):
        own = service.add_block(BlocklistEntry.Kind.IP, '102.176.2.1', self.analyst_a, organization=self.org_a)
        platform_entry = service.add_block(BlocklistEntry.Kind.IP, '102.176.2.2', None, 'platform')
        self.client.force_login(self.analyst_b)
        page = self.client.get('/console/fraud/blocklist/')
        self.assertNotContains(page, '102.176.2.1')
        self.assertNotContains(page, '102.176.2.2')
        for entry in (own, platform_entry):
            response = self.client.post('/console/fraud/blocklist/', {'action': 'deactivate', 'entry': entry.pk})
            self.assertEqual(response.status_code, 404)
        # Additions by a tenant analyst are scoped to their organization, even if
        # they try to target another one or the whole platform.
        self.client.post('/console/fraud/blocklist/', {'kind': 'IP', 'value': '102.176.3.3', 'organization': ''})
        self.assertEqual(BlocklistEntry.objects.get(value='102.176.3.3').organization, self.org_b)
        response = self.client.post('/console/fraud/blocklist/', {'kind': 'IP', 'value': '102.176.3.4',
                                                                   'organization': self.org_a.pk})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(BlocklistEntry.objects.filter(value='102.176.3.4').exists())
        own.refresh_from_db()
        platform_entry.refresh_from_db()
        self.assertTrue(own.is_active and platform_entry.is_active)
