from django.conf import settings
from django.db import models


class FraudEvent(models.Model):
    """A risk assessment worth keeping (score >= monitor threshold) or an
    anomaly found by the background scanner. Suspicious activity is held for
    human review rather than silently deleted."""

    class Kind(models.TextChoices):
        PAYMENT = 'PAYMENT', 'Payment'
        VOTE = 'VOTE', 'Vote'
        VOTER_LOGIN = 'VOTER_LOGIN', 'Voter sign-in'
        STAFF_LOGIN = 'STAFF_LOGIN', 'Staff sign-in'
        REGISTRATION = 'REGISTRATION', 'Registration'
        ANOMALY = 'ANOMALY', 'Traffic anomaly'
        CHARGEBACK = 'CHARGEBACK', 'Chargeback / reversal'

    class Decision(models.TextChoices):
        ALLOW = 'ALLOW', 'Allow (normal)'
        MONITOR = 'MONITOR', 'Monitor'
        CHALLENGE = 'CHALLENGE', 'Challenge / verify'
        HOLD = 'HOLD', 'Hold for review'

    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        CONFIRMED = 'CONFIRMED', 'Confirmed fraud'
        DISMISSED = 'DISMISSED', 'Dismissed (legitimate)'

    kind = models.CharField(max_length=12, choices=Kind.choices)
    decision = models.CharField(max_length=10, choices=Decision.choices)
    score = models.PositiveSmallIntegerField()
    signals = models.JSONField(default=list)
    event = models.ForeignKey('voting.Event', on_delete=models.CASCADE, null=True, blank=True, related_name='fraud_events')
    payment = models.ForeignKey('payments.Payment', on_delete=models.CASCADE, null=True, blank=True, related_name='fraud_events')
    candidate = models.ForeignKey('voting.Candidate', on_delete=models.SET_NULL, null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_hash = models.CharField(max_length=64, blank=True)
    subject = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.kind} {self.score} {self.decision}'


class BlocklistEntry(models.Model):
    class Kind(models.TextChoices):
        IP = 'IP', 'IP address'
        CIDR = 'CIDR', 'IP range (CIDR)'
        EMAIL = 'EMAIL', 'Email address'
        EMAIL_DOMAIN = 'EMAIL_DOMAIN', 'Email domain'
        PHONE = 'PHONE', 'Phone number'
        DEVICE = 'DEVICE', 'Device'
        CARD = 'CARD', 'Card signature'
        ANONYMIZER = 'ANONYMIZER', 'Tor exit / proxy / VPN range (CIDR)'

    kind = models.CharField(max_length=12, choices=Kind.choices)
    # Emails and phones are stored as blind indexes, never in clear.
    value = models.CharField(max_length=128)
    # Empty = platform-wide (platform admins only). Otherwise the entry only
    # applies to that organization's events, so one tenant can never block
    # another tenant's voters or see its entries.
    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, null=True, blank=True,
                                     related_name='+')
    reason = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['kind', 'value'])]
        verbose_name_plural = 'blocklist entries'

    def __str__(self):
        return f'{self.kind}: {self.value}'
