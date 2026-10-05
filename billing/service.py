"""Organization subscriptions, usage metering, plan limits, feature flags and invoicing."""
import calendar
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core import audit

from .models import Coupon, FeatureFlag, FeatureOverride, Invoice, InvoiceLine, Plan, Subscription, UsageRecord

CENT = Decimal('0.01')

PLAN_DEFINITIONS = {
    'FREE': {
        'name': 'Free', 'sort_order': 0, 'description': 'Small polls and trial elections.',
        'monthly_price': '0', 'annual_price': '0', 'price_per_election': '0', 'price_per_voter': '0',
        'included_voters': 1000,
        'limits': {'max_active_elections': 3, 'max_voters_per_election': 1000, 'max_staff': 3},
        'features': ['paid_voting', 'institutional_elections', 'exports'],
    },
    'PROFESSIONAL': {
        'name': 'Professional', 'sort_order': 1, 'description': 'Universities, associations and award shows.',
        'monthly_price': '500', 'annual_price': '5000', 'price_per_election': '100', 'price_per_voter': '0.05',
        'included_voters': 5000,
        'limits': {'max_active_elections': 10, 'max_voters_per_election': 20000, 'max_staff': 15},
        'features': ['paid_voting', 'institutional_elections', 'exports', 'sso', 'sms_notifications',
                     'constituency_results', 'custom_branding', 'api'],
    },
    'ENTERPRISE': {
        'name': 'Enterprise', 'sort_order': 2, 'description': 'Large institutions and broadcasters.',
        'monthly_price': '2500', 'annual_price': '25000', 'price_per_election': '0', 'price_per_voter': '0.02',
        'included_voters': 50000,
        'limits': {'max_active_elections': 100, 'max_voters_per_election': 250000, 'max_staff': 200},
        'features': ['paid_voting', 'institutional_elections', 'exports', 'sso', 'ldap', 'sms_notifications',
                     'whatsapp_notifications', 'constituency_results', 'custom_branding', 'api', 'dual_approval'],
    },
    'HIGH_ASSURANCE': {
        'name': 'Government / High Assurance', 'sort_order': 3,
        'description': 'Trustee-held ballot keys, independent audit and legal hold.',
        'monthly_price': '7500', 'annual_price': '75000', 'price_per_election': '0', 'price_per_voter': '0.01',
        'included_voters': 500000,
        'limits': {'max_active_elections': None, 'max_voters_per_election': None, 'max_staff': None},
        'features': ['paid_voting', 'institutional_elections', 'exports', 'sso', 'ldap', 'sms_notifications',
                     'whatsapp_notifications', 'constituency_results', 'custom_branding', 'api', 'dual_approval',
                     'trustee_keys', 'independent_audit', 'legal_hold'],
    },
}

SMS_UNIT_PRICE = Decimal('0.05')


class BillingLimitError(Exception):
    pass


def sync_plans():
    for code, spec in PLAN_DEFINITIONS.items():
        Plan.objects.update_or_create(code=code, defaults={
            'name': spec['name'], 'description': spec['description'], 'sort_order': spec['sort_order'],
            'monthly_price': Decimal(spec['monthly_price']), 'annual_price': Decimal(spec['annual_price']),
            'price_per_election': Decimal(spec['price_per_election']), 'price_per_voter': Decimal(spec['price_per_voter']),
            'included_voters': spec['included_voters'], 'limits': spec['limits'], 'features': spec['features'],
            'currency': settings.DEFAULT_CURRENCY,
        })


def _add_months(dt, months):
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def get_subscription(organization):
    """Every organization has a subscription; new ones start on Free."""
    subscription = getattr(organization, '_fv_subscription', None)
    if subscription is not None:
        return subscription
    try:
        subscription = organization.subscription
    except Subscription.DoesNotExist:
        plan = Plan.objects.filter(code='FREE').first()
        if plan is None:
            sync_plans()
            plan = Plan.objects.get(code='FREE')
        now = timezone.now()
        subscription, _ = Subscription.objects.get_or_create(organization=organization, defaults={
            'plan': plan, 'status': Subscription.Status.ACTIVE, 'current_period_start': now,
            'current_period_end': _add_months(now, 1),
        })
    organization._fv_subscription = subscription
    return subscription


def start_trial(organization, plan_code, actor=None):
    plan = Plan.objects.get(code=plan_code)
    subscription = get_subscription(organization)
    now = timezone.now()
    subscription.plan = plan
    subscription.status = Subscription.Status.TRIALING
    subscription.trial_ends_at = now + timedelta(days=settings.BILLING_TRIAL_DAYS)
    subscription.current_period_start = now
    subscription.current_period_end = subscription.trial_ends_at
    subscription.save()
    audit.record('BILLING_TRIAL_STARTED', actor=actor, organization=organization, target=subscription,
                 summary=f'{plan.name} trial started')
    return subscription


def change_plan(organization, plan_code, actor, cycle=Subscription.Cycle.MONTHLY, coupon_code=''):
    plan = Plan.objects.get(code=plan_code)
    subscription = get_subscription(organization)
    old = subscription.plan.code
    subscription.plan = plan
    subscription.billing_cycle = cycle
    if coupon_code:
        coupon = Coupon.objects.filter(code=coupon_code.strip().upper(), is_active=True).first()
        if coupon is None or (coupon.valid_until and coupon.valid_until < timezone.now()) or \
                (coupon.max_redemptions is not None and coupon.redemptions >= coupon.max_redemptions):
            raise BillingLimitError('That coupon is not valid.')
        subscription.coupon = coupon
        subscription.coupon_applied_at = timezone.now()
        Coupon.objects.filter(pk=coupon.pk).update(redemptions=coupon.redemptions + 1)
    if plan.monthly_price == 0:
        subscription.status = Subscription.Status.ACTIVE
    subscription.save()
    organization._fv_subscription = subscription
    audit.record('BILLING_PLAN_CHANGED', actor=actor, organization=organization, target=subscription,
                 changes={'plan': {'old': old, 'new': plan.code}})
    invoice = None
    if plan.monthly_price > 0 and subscription.status != Subscription.Status.TRIALING:
        now = timezone.now()
        end = _add_months(now, 12 if cycle == Subscription.Cycle.ANNUAL else 1)
        invoice = generate_invoice(organization, now, end, actor=actor, include_usage=False)
        subscription.status = Subscription.Status.PAST_DUE
        subscription.save(update_fields=['status'])
    return subscription, invoice


def feature_enabled(organization, key):
    if organization is None:
        return True
    override = FeatureOverride.objects.filter(flag__key=key, organization=organization).first()
    if override is not None:
        return override.enabled
    subscription = get_subscription(organization)
    if subscription.status in (Subscription.Status.ACTIVE, Subscription.Status.TRIALING) and key in subscription.plan.features:
        return True
    flag = FeatureFlag.objects.filter(key=key).first()
    return bool(flag and flag.enabled_globally)


def limit_value(organization, key):
    return get_subscription(organization).plan.limits.get(key)


def check_limit(organization, key, value):
    limit = limit_value(organization, key)
    if limit is not None and value > limit:
        plan = get_subscription(organization).plan
        raise BillingLimitError(f'Your {plan.name} plan allows {limit} for "{key.replace("_", " ")}". '
                                f'Upgrade the plan to go further.')


def record_usage(organization, metric, quantity=1, event=None):
    if organization is None or quantity <= 0:
        return None
    return UsageRecord.objects.create(organization=organization, metric=metric, quantity=quantity, event=event)


def usage_summary(organization, start, end):
    rows = UsageRecord.objects.filter(organization=organization, recorded_at__gte=start, recorded_at__lt=end) \
        .values('metric').annotate(total=Sum('quantity'))
    return {row['metric']: row['total'] for row in rows}


def _money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _next_invoice_number():
    prefix = timezone.now().strftime('INV-%Y%m-')
    last = Invoice.objects.filter(number__startswith=prefix).order_by('-number').values_list('number', flat=True).first()
    sequence = int(last.rsplit('-', 1)[-1]) + 1 if last else 1
    return f'{prefix}{sequence:05d}'


def generate_invoice(organization, period_start, period_end, actor=None, include_usage=True):
    subscription = get_subscription(organization)
    plan = subscription.plan
    with transaction.atomic():
        invoice = Invoice.objects.create(
            number=_next_invoice_number(), organization=organization, currency=plan.currency,
            period_start=period_start, period_end=period_end, created_by=actor,
            vat_rate=Decimal(settings.BILLING_VAT_RATE), levy_rate=Decimal(settings.BILLING_LEVY_RATE),
        )
        lines = []
        base = plan.annual_price if subscription.billing_cycle == Subscription.Cycle.ANNUAL else plan.monthly_price
        if base > 0:
            lines.append(('%s plan (%s)' % (plan.name, subscription.get_billing_cycle_display()), 1, base, ''))
        if include_usage:
            usage = usage_summary(organization, period_start, period_end)
            elections = usage.get('ELECTION_CREATED', 0)
            if elections and plan.price_per_election > 0:
                lines.append(('Elections created', elections, plan.price_per_election, 'ELECTION_CREATED'))
            voters = usage.get('VOTERS_IMPORTED', 0)
            billable = max(0, voters - plan.included_voters)
            if billable and plan.price_per_voter > 0:
                lines.append((f'Voters beyond the {plan.included_voters} included', billable, plan.price_per_voter,
                              'VOTERS_IMPORTED'))
            sms = usage.get('SMS_SENT', 0)
            if sms:
                lines.append(('SMS messages', sms, SMS_UNIT_PRICE, 'SMS_SENT'))
        subtotal = Decimal('0')
        for description, quantity, unit_price, metric in lines:
            amount = _money(Decimal(quantity) * Decimal(unit_price))
            InvoiceLine.objects.create(invoice=invoice, description=description, quantity=quantity,
                                       unit_price=unit_price, amount=amount)
            subtotal += amount
        discount = Decimal('0')
        coupon = subscription.coupon
        if coupon and coupon.is_active:
            months_used = 0
            if subscription.coupon_applied_at:
                months_used = (period_start.year - subscription.coupon_applied_at.year) * 12 + \
                    period_start.month - subscription.coupon_applied_at.month
            if coupon.duration_months is None or months_used < coupon.duration_months:
                discount = subtotal * coupon.value / 100 if coupon.kind == Coupon.Kind.PERCENT else coupon.value
                discount = min(_money(discount), subtotal)
                invoice.coupon = coupon
        taxable = subtotal - discount
        levy = _money(taxable * invoice.levy_rate / 100)
        vat = _money((taxable + levy) * invoice.vat_rate / 100)
        invoice.subtotal = _money(subtotal)
        invoice.discount = discount
        invoice.levy = levy
        invoice.vat = vat
        invoice.total = _money(taxable + levy + vat)
        now = timezone.now()
        invoice.status = Invoice.Status.OPEN if invoice.total > 0 else Invoice.Status.PAID
        invoice.issued_at = now
        invoice.due_at = now + timedelta(days=7)
        if invoice.total == 0:
            invoice.paid_at = now
        invoice.save()
        if include_usage:
            UsageRecord.objects.filter(organization=organization, recorded_at__gte=period_start,
                                       recorded_at__lt=period_end, invoice__isnull=True).update(invoice=invoice)
        audit.record('BILLING_INVOICE_ISSUED', actor=actor, organization=organization, target=invoice,
                     summary=f'Invoice {invoice.number} issued for {invoice.total} {invoice.currency}')
    return invoice


def mark_invoice_paid(invoice, payment=None):
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.status == Invoice.Status.PAID:
            return invoice
        invoice.status = Invoice.Status.PAID
        invoice.paid_at = timezone.now()
        invoice.save(update_fields=['status', 'paid_at'])
        subscription = get_subscription(invoice.organization)
        subscription.status = Subscription.Status.ACTIVE
        subscription.current_period_start = invoice.period_start
        subscription.current_period_end = max(subscription.current_period_end, invoice.period_end)
        subscription.save()
        audit.record('BILLING_INVOICE_PAID', organization=invoice.organization, target=invoice,
                     summary=f'Invoice {invoice.number} paid', metadata={'payment': payment.reference if payment else ''})
    return invoice


def renew_due_subscriptions(now=None):
    now = now or timezone.now()
    issued = 0
    for subscription in Subscription.objects.select_related('plan', 'organization').filter(
            current_period_end__lte=now, status__in=[Subscription.Status.ACTIVE, Subscription.Status.TRIALING,
                                                    Subscription.Status.PAST_DUE]):
        if subscription.cancel_at_period_end:
            subscription.status = Subscription.Status.CANCELED
            subscription.save(update_fields=['status'])
            continue
        start = subscription.current_period_end
        end = _add_months(start, 12 if subscription.billing_cycle == Subscription.Cycle.ANNUAL else 1)
        invoice = generate_invoice(subscription.organization, start, end)
        subscription.current_period_start, subscription.current_period_end = start, end
        if subscription.status == Subscription.Status.TRIALING:
            subscription.trial_ends_at = None
        subscription.status = Subscription.Status.ACTIVE if invoice.status == Invoice.Status.PAID else Subscription.Status.PAST_DUE
        subscription.save()
        issued += 1
    return issued
