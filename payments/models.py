import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.crypto import access_code, blind_index
from core.fields import EncryptedTextField
from core.models import AppendOnlyQuerySet


class VotePackage(models.Model):
    """Vote bundle, e.g. "10 votes for GHS 9" - optionally a time-boxed promotion."""

    event = models.ForeignKey('voting.Event', on_delete=models.CASCADE, related_name='vote_packages')
    name = models.CharField(max_length=80)
    votes = models.PositiveIntegerField()
    bonus_votes = models.PositiveIntegerField(default=0)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    badge = models.CharField(max_length=40, blank=True)
    is_active = models.BooleanField(default=True)
    is_promotional = models.BooleanField(default=False)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    max_per_payer = models.PositiveIntegerField(null=True, blank=True)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['display_order', 'price']

    def __str__(self):
        return f'{self.name} ({self.total_votes} votes)'

    @property
    def total_votes(self):
        return self.votes + self.bonus_votes

    def is_available(self, now=None):
        now = now or timezone.now()
        if not self.is_active:
            return False
        if self.starts_at and now < self.starts_at:
            return False
        return not (self.ends_at and now >= self.ends_at)


class DiscountCode(models.Model):
    class Kind(models.TextChoices):
        PERCENT = 'PERCENT', 'Percent off'
        FIXED = 'FIXED', 'Fixed amount off'
        BONUS_VOTES = 'BONUS_VOTES', 'Bonus votes'

    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, null=True, blank=True)
    event = models.ForeignKey('voting.Event', on_delete=models.CASCADE, null=True, blank=True, related_name='discount_codes')
    code = models.CharField(max_length=40, unique=True)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.PERCENT)
    value = models.DecimalField(max_digits=10, decimal_places=2)
    max_redemptions = models.PositiveIntegerField(null=True, blank=True)
    per_payer_limit = models.PositiveIntegerField(null=True, blank=True)
    min_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    redemptions_count = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        self.code = (self.code or '').strip().upper()
        super().save(*args, **kwargs)

    def is_available(self, now=None):
        now = now or timezone.now()
        if not self.is_active:
            return False
        if self.starts_at and now < self.starts_at:
            return False
        if self.ends_at and now >= self.ends_at:
            return False
        return not (self.max_redemptions is not None and self.redemptions_count >= self.max_redemptions)


class Payment(models.Model):
    class Status(models.TextChoices):
        INITIALIZED = 'INITIALIZED', 'Initialized'
        PENDING = 'PENDING', 'Pending (customer at gateway)'
        SUCCESS = 'SUCCESS', 'Successful'
        FAILED = 'FAILED', 'Failed'
        ABANDONED = 'ABANDONED', 'Abandoned'
        REVERSED = 'REVERSED', 'Reversed'
        REFUNDED = 'REFUNDED', 'Refunded'
        PARTIALLY_REFUNDED = 'PARTIALLY_REFUNDED', 'Partially refunded'
        DISPUTED = 'DISPUTED', 'Chargeback / dispute'

    class Purpose(models.TextChoices):
        VOTE = 'VOTE', 'Votes'
        INVOICE = 'INVOICE', 'Subscription invoice'

    FINAL_STATUSES = (Status.FAILED, Status.ABANDONED, Status.REVERSED, Status.REFUNDED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=40, unique=True)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    purpose = models.CharField(max_length=10, choices=Purpose.choices, default=Purpose.VOTE)
    event = models.ForeignKey('voting.Event', on_delete=models.PROTECT, null=True, blank=True, related_name='payments')
    candidate = models.ForeignKey('voting.Candidate', on_delete=models.PROTECT, null=True, blank=True, related_name='payments')
    package = models.ForeignKey(VotePackage, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    discount = models.ForeignKey(DiscountCode, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    invoice = models.ForeignKey('billing.Invoice', on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    votes = models.PositiveIntegerField(default=0)
    bonus_votes = models.PositiveIntegerField(default=0)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default='GHS')
    payer_email = EncryptedTextField(blank=True, default='')
    payer_email_index = models.CharField(max_length=64, blank=True, db_index=True)
    payer_phone = EncryptedTextField(blank=True, default='')
    payer_phone_index = models.CharField(max_length=64, blank=True, db_index=True)
    payer_name = models.CharField(max_length=150, blank=True)
    channel = models.CharField(max_length=30, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INITIALIZED, db_index=True)
    gateway_status = models.CharField(max_length=30, blank=True)
    gateway_id = models.CharField(max_length=40, blank=True)
    authorization_url = models.URLField(max_length=500, blank=True)
    access_code = models.CharField(max_length=100, blank=True)
    card_signature = models.CharField(max_length=100, blank=True, db_index=True)
    card_country = models.CharField(max_length=4, blank=True)
    card_last4 = models.CharField(max_length=4, blank=True)
    card_brand = models.CharField(max_length=30, blank=True)
    card_bank = models.CharField(max_length=80, blank=True)
    gateway_ip = models.GenericIPAddressField(null=True, blank=True)
    fees = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    gateway_paid_at = models.DateTimeField(null=True, blank=True)
    risk_score = models.PositiveSmallIntegerField(null=True, blank=True)
    risk_decision = models.CharField(max_length=10, blank=True)
    held = models.BooleanField(default=False)
    hold_reason = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_hash = models.CharField(max_length=64, blank=True, db_index=True)
    user_agent = models.CharField(max_length=300, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    credited_at = models.DateTimeField(null=True, blank=True)
    votes_credited = models.BooleanField(default=False)
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['idempotency_key'], condition=Q(idempotency_key__isnull=False),
                                    name='uniq_payment_idempotency_key'),
        ]
        indexes = [models.Index(fields=['event', 'status']), models.Index(fields=['status', 'created_at'])]

    def __str__(self):
        return f'{self.reference} {self.amount} {self.currency} {self.status}'

    @staticmethod
    def new_reference():
        return f'FV-{access_code(14)}'

    @property
    def total_votes(self):
        return self.votes + self.bonus_votes

    @property
    def amount_minor(self):
        return int((self.amount * 100).quantize(Decimal('1')))

    def set_payer(self, email='', phone=''):
        from core.utils import normalize_phone

        email = (email or '').strip().lower()
        phone = normalize_phone(phone)
        self.payer_email = email
        self.payer_email_index = blind_index(email, 'email') if email else ''
        self.payer_phone = phone
        self.payer_phone_index = blind_index(phone, 'phone') if phone else ''


class PaymentEvent(models.Model):
    """Append-only history of everything that happened to a payment."""

    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name='history')
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, blank=True)
    source = models.CharField(max_length=20)
    message = models.CharField(max_length=500, blank=True)
    data = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ['created_at', 'id']

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionDenied('Payment history is append-only.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied('Payment history is append-only.')


class WebhookEvent(models.Model):
    class Status(models.TextChoices):
        RECEIVED = 'RECEIVED', 'Received'
        PROCESSED = 'PROCESSED', 'Processed'
        IGNORED = 'IGNORED', 'Ignored'
        DUPLICATE = 'DUPLICATE', 'Duplicate (replay)'
        FAILED = 'FAILED', 'Failed'

    provider = models.CharField(max_length=20, default='paystack')
    event_type = models.CharField(max_length=60)
    payload_hash = models.CharField(max_length=64, unique=True)
    reference = models.CharField(max_length=100, blank=True, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RECEIVED)
    error = models.TextField(blank=True)
    payload = models.JSONField(default=dict)
    attempts = models.PositiveSmallIntegerField(default=1)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-received_at']


class Refund(models.Model):
    class Status(models.TextChoices):
        REQUESTED = 'REQUESTED', 'Requested (awaiting approval)'
        APPROVED = 'APPROVED', 'Approved'
        PROCESSING = 'PROCESSING', 'Processing at gateway'
        PROCESSED = 'PROCESSED', 'Processed'
        FAILED = 'FAILED', 'Failed'
        REJECTED = 'REJECTED', 'Rejected'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name='refunds')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.REQUESTED)
    reverse_votes = models.BooleanField(default=True)
    gateway_refund_id = models.CharField(max_length=40, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    requested_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ['-requested_at']


class ReconciliationRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = 'RUNNING', 'Running'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'

    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    checked_count = models.PositiveIntegerField(default=0)
    discrepancy_count = models.PositiveIntegerField(default=0)
    resolved_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']


class ReconciliationItem(models.Model):
    class Kind(models.TextChoices):
        MISSING_LOCALLY = 'MISSING_LOCALLY', 'Paid at gateway, unknown locally'
        STATUS_MISMATCH = 'STATUS_MISMATCH', 'Status differs from gateway'
        AMOUNT_MISMATCH = 'AMOUNT_MISMATCH', 'Amount/currency differs from gateway'
        NOT_AT_GATEWAY = 'NOT_AT_GATEWAY', 'Successful locally, not found at gateway'
        NOT_CREDITED = 'NOT_CREDITED', 'Paid but votes not credited'

    class Resolution(models.TextChoices):
        AUTO_RESOLVED = 'AUTO_RESOLVED', 'Auto-resolved'
        NEEDS_REVIEW = 'NEEDS_REVIEW', 'Needs review'
        RESOLVED = 'RESOLVED', 'Resolved manually'
        IGNORED = 'IGNORED', 'Ignored'

    run = models.ForeignKey(ReconciliationRun, on_delete=models.CASCADE, related_name='items')
    payment = models.ForeignKey(Payment, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reference = models.CharField(max_length=100)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    local_status = models.CharField(max_length=20, blank=True)
    gateway_status = models.CharField(max_length=30, blank=True)
    local_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    gateway_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    resolution = models.CharField(max_length=14, choices=Resolution.choices, default=Resolution.NEEDS_REVIEW)
    note = models.TextField(blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
