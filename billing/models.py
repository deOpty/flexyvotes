from decimal import Decimal

from django.conf import settings
from django.db import models


class Plan(models.Model):
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)
    currency = models.CharField(max_length=3, default='GHS')
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    annual_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    price_per_election = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    price_per_voter = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('0'))
    included_voters = models.PositiveIntegerField(default=0)
    limits = models.JSONField(default=dict, blank=True)
    features = models.JSONField(default=list, blank=True)
    is_public = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort_order']

    def __str__(self):
        return self.name


class Subscription(models.Model):
    class Status(models.TextChoices):
        TRIALING = 'TRIALING', 'Trial'
        ACTIVE = 'ACTIVE', 'Active'
        PAST_DUE = 'PAST_DUE', 'Past due'
        CANCELED = 'CANCELED', 'Canceled'

    class Cycle(models.TextChoices):
        MONTHLY = 'MONTHLY', 'Monthly'
        ANNUAL = 'ANNUAL', 'Annual'

    organization = models.OneToOneField('core.Organization', on_delete=models.CASCADE, related_name='subscription')
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name='subscriptions')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.TRIALING)
    billing_cycle = models.CharField(max_length=8, choices=Cycle.choices, default=Cycle.MONTHLY)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()
    cancel_at_period_end = models.BooleanField(default=False)
    coupon = models.ForeignKey('Coupon', on_delete=models.SET_NULL, null=True, blank=True)
    coupon_applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.organization} - {self.plan.code} ({self.status})'


class FeatureFlag(models.Model):
    key = models.SlugField(max_length=60, unique=True)
    description = models.CharField(max_length=255, blank=True)
    enabled_globally = models.BooleanField(default=False)

    def __str__(self):
        return self.key


class FeatureOverride(models.Model):
    flag = models.ForeignKey(FeatureFlag, on_delete=models.CASCADE, related_name='overrides')
    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, related_name='feature_overrides')
    enabled = models.BooleanField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['flag', 'organization'], name='uniq_feature_override')]


class Coupon(models.Model):
    class Kind(models.TextChoices):
        PERCENT = 'PERCENT', 'Percent off'
        FIXED = 'FIXED', 'Fixed amount off'

    code = models.CharField(max_length=40, unique=True)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.PERCENT)
    value = models.DecimalField(max_digits=10, decimal_places=2)
    duration_months = models.PositiveSmallIntegerField(null=True, blank=True,
                                                       help_text='Empty = applies forever.')
    max_redemptions = models.PositiveIntegerField(null=True, blank=True)
    redemptions = models.PositiveIntegerField(default=0)
    valid_until = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        self.code = (self.code or '').strip().upper()
        super().save(*args, **kwargs)


class UsageRecord(models.Model):
    class Metric(models.TextChoices):
        ELECTION_CREATED = 'ELECTION_CREATED', 'Election created'
        VOTERS_IMPORTED = 'VOTERS_IMPORTED', 'Voters on roll'
        BALLOTS_CAST = 'BALLOTS_CAST', 'Ballots cast'
        SMS_SENT = 'SMS_SENT', 'SMS sent'
        EMAILS_SENT = 'EMAILS_SENT', 'Emails sent'

    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, related_name='usage_records')
    metric = models.CharField(max_length=20, choices=Metric.choices)
    quantity = models.PositiveIntegerField(default=1)
    event = models.ForeignKey('voting.Event', on_delete=models.SET_NULL, null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True, db_index=True)
    invoice = models.ForeignKey('Invoice', on_delete=models.SET_NULL, null=True, blank=True, related_name='usage')

    class Meta:
        indexes = [models.Index(fields=['organization', 'metric', 'recorded_at'])]


class Invoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        OPEN = 'OPEN', 'Open (awaiting payment)'
        PAID = 'PAID', 'Paid'
        VOID = 'VOID', 'Void'

    number = models.CharField(max_length=30, unique=True)
    organization = models.ForeignKey('core.Organization', on_delete=models.PROTECT, related_name='invoices')
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.DRAFT)
    currency = models.CharField(max_length=3, default='GHS')
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    vat = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    levy_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    levy = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    coupon = models.ForeignKey(Coupon, on_delete=models.SET_NULL, null=True, blank=True)
    issued_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.number


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='lines')
    description = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('1'))
    unit_price = models.DecimalField(max_digits=12, decimal_places=4)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
