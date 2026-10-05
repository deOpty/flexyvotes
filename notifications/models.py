import uuid

from django.conf import settings
from django.db import models

from core.fields import EncryptedJSONField, EncryptedTextField


class Notification(models.Model):
    """Outbox row. Created inside the business transaction, delivered later by
    a background worker - never sent synchronously on the voting path."""

    class Channel(models.TextChoices):
        EMAIL = 'EMAIL', 'Email'
        SMS = 'SMS', 'SMS'
        WHATSAPP = 'WHATSAPP', 'WhatsApp'
        IN_APP = 'IN_APP', 'In-app / push'

    class Status(models.TextChoices):
        QUEUED = 'QUEUED', 'Queued'
        SENT = 'SENT', 'Sent'
        FAILED = 'FAILED', 'Failed'
        SKIPPED = 'SKIPPED', 'Skipped (channel not configured)'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('core.Organization', on_delete=models.SET_NULL, null=True, blank=True)
    event = models.ForeignKey('voting.Event', on_delete=models.SET_NULL, null=True, blank=True, related_name='notifications')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
                             related_name='notifications')
    channel = models.CharField(max_length=10, choices=Channel.choices)
    template = models.CharField(max_length=60)
    recipient = EncryptedTextField(blank=True, default='')
    recipient_hint = models.CharField(max_length=120, blank=True)
    # Context may contain one-time credentials: encrypted, and wiped on send.
    context = EncryptedJSONField(null=True, blank=True)
    subject = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.QUEUED, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    dedupe_key = models.CharField(max_length=200, null=True, blank=True, unique=True)
    provider_message_id = models.CharField(max_length=120, blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.template} via {self.channel} ({self.status})'
