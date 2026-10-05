from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from billing import service
from billing.models import Coupon, FeatureFlag, FeatureOverride, Invoice, Plan, Subscription, UsageRecord
from core.tests.factories import PASSWORD, make_org, make_user


@override_settings(BILLING_VAT_RATE='15.0', BILLING_LEVY_RATE='6.0')
class BillingTests(TestCase):
    def setUp(self):
        self.admin = make_user('billing_admin')
        self.org = make_org('Billing Org', admin=self.admin)

    def test_plans_seeded_and_default_free_subscription(self):
        self.assertEqual(set(Plan.objects.values_list('code', flat=True)),
                         {'FREE', 'PROFESSIONAL', 'ENTERPRISE', 'HIGH_ASSURANCE'})
        subscription = service.get_subscription(self.org)
        self.assertEqual(subscription.plan.code, 'FREE')
        self.assertFalse(service.feature_enabled(self.org, 'sso'))
        self.assertTrue(service.feature_enabled(self.org, 'exports'))

    def test_feature_flag_override(self):
        flag = FeatureFlag.objects.create(key='sso')
        FeatureOverride.objects.create(flag=flag, organization=self.org, enabled=True)
        self.assertTrue(service.feature_enabled(self.org, 'sso'))

    def test_limits(self):
        with self.assertRaises(service.BillingLimitError):
            service.check_limit(self.org, 'max_active_elections', 4)
        service.change_plan(self.org, 'HIGH_ASSURANCE', self.admin)
        self.org._fv_subscription = None
        service.check_limit(self.org, 'max_active_elections', 10_000)

    def test_invoice_with_usage_coupon_levies_and_vat(self):
        now = timezone.now()
        service.change_plan(self.org, 'PROFESSIONAL', self.admin, coupon_code='')
        Coupon.objects.create(code='LAUNCH20', kind='PERCENT', value=Decimal('20'))
        service.change_plan(self.org, 'PROFESSIONAL', self.admin, coupon_code='launch20')
        UsageRecord.objects.create(organization=self.org, metric='ELECTION_CREATED', quantity=2)
        UsageRecord.objects.create(organization=self.org, metric='VOTERS_IMPORTED', quantity=6000)
        UsageRecord.objects.create(organization=self.org, metric='SMS_SENT', quantity=100)
        invoice = service.generate_invoice(self.org, now - timedelta(days=1), now + timedelta(days=1))
        # 500 plan + 2*100 elections + 1000*0.05 voters + 100*0.05 sms = 755
        self.assertEqual(invoice.subtotal, Decimal('755.00'))
        self.assertEqual(invoice.discount, Decimal('151.00'))
        self.assertEqual(invoice.levy, Decimal('36.24'))
        self.assertEqual(invoice.vat, Decimal('96.04'))
        self.assertEqual(invoice.total, Decimal('736.28'))
        self.assertEqual(invoice.status, Invoice.Status.OPEN)
        self.assertTrue(invoice.number.startswith('INV-'))
        service.mark_invoice_paid(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PAID)
        self.assertEqual(Subscription.objects.get(organization=self.org).status, Subscription.Status.ACTIVE)

    def test_trial_and_renewal(self):
        subscription = service.start_trial(self.org, 'PROFESSIONAL', self.admin)
        self.assertEqual(subscription.status, Subscription.Status.TRIALING)
        Subscription.objects.filter(pk=subscription.pk).update(current_period_end=timezone.now() - timedelta(minutes=1))
        self.assertEqual(service.renew_due_subscriptions(), 1)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Subscription.Status.PAST_DUE)
        self.assertEqual(self.org.invoices.count(), 1)

    def test_billing_pages(self):
        self.client.login(username='billing_admin', password=PASSWORD)
        self.assertEqual(self.client.get(reverse('billing:home')).status_code, 200)
        self.client.post(reverse('billing:home'), {'action': 'change', 'plan': 'PROFESSIONAL', 'cycle': 'MONTHLY'})
        invoice = Invoice.objects.get(organization=self.org)
        self.assertEqual(self.client.get(reverse('billing:invoice', args=[invoice.pk])).status_code, 200)
        pdf = self.client.get(reverse('billing:invoice', args=[invoice.pk]) + '?format=pdf')
        self.assertEqual(pdf['Content-Type'], 'application/pdf')
        stranger = make_user('stranger')
        make_org('Stranger Org', admin=stranger)
        self.client.logout()
        self.client.login(username='stranger', password=PASSWORD)
        self.assertEqual(self.client.get(reverse('billing:invoice', args=[invoice.pk])).status_code, 403)
