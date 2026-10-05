import uuid

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from .fields import EncryptedJSONField, EncryptedTextField


# ---------------------------------------------------------------------------
# Multi-tenancy
# ---------------------------------------------------------------------------
class Organization(models.Model):
    class Kind(models.TextChoices):
        UNIVERSITY = 'UNIVERSITY', 'University / school'
        COMPANY = 'COMPANY', 'Company'
        ASSOCIATION = 'ASSOCIATION', 'Association / union'
        PROFESSIONAL_BODY = 'PROFESSIONAL_BODY', 'Professional body'
        MEDIA = 'MEDIA', 'Media / entertainment'
        GOVERNMENT = 'GOVERNMENT', 'Government'
        OTHER = 'OTHER', 'Other'

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=80, unique=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.OTHER)
    contact_email = models.EmailField(blank=True)
    default_timezone = models.CharField(max_length=64, default='Africa/Accra')
    default_currency = models.CharField(max_length=3, default='GHS')
    default_language = models.CharField(max_length=8, default='en')
    is_active = models.BooleanField(default=True)
    # Organizer accounts get a personal organization on approval.
    is_personal = models.BooleanField(default=False)
    # Institutional identity integration. Secrets inside are encrypted.
    sso_config = EncryptedJSONField(blank=True, null=True)
    ldap_config = EncryptedJSONField(blank=True, null=True)
    directory_config = EncryptedJSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:60] or 'org'
            slug, n = base, 1
            while Organization.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                n += 1
                slug = f'{base}-{n}'
            self.slug = slug
        super().save(*args, **kwargs)


class Role(models.Model):
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)
    permissions = models.JSONField(default=list)
    is_system = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class RoleAssignment(models.Model):
    """Grants a role to a user, scoped platform-wide, to an organization, or
    to a single election within an organization."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='role_assignments')
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='assignments')
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True, related_name='role_assignments')
    event = models.ForeignKey('voting.Event', on_delete=models.CASCADE, null=True, blank=True, related_name='role_assignments')
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'role', 'organization', 'event'], name='uniq_role_assignment'),
        ]

    def __str__(self):
        scope = self.event or self.organization or 'platform'
        return f'{self.user} - {self.role.code} @ {scope}'


# ---------------------------------------------------------------------------
# Tamper-evident audit log
# ---------------------------------------------------------------------------
class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise PermissionDenied('This log is append-only.')

    def delete(self):
        raise PermissionDenied('This log is append-only.')


class AuditChainHead(models.Model):
    chain = models.CharField(max_length=64, unique=True)
    seq = models.BigIntegerField(default=0)
    last_hash = models.CharField(max_length=64, default='0' * 64)


class AuditEvent(models.Model):
    """One link in a per-chain hash chain. Never updated or deleted (enforced
    in the ORM and by a database trigger on PostgreSQL).

    Actor/organization/election are stored as plain ids plus labels instead
    of foreign keys so that deleting a referenced row can never require
    rewriting (and so invalidating) evidence.
    """

    class Result(models.TextChoices):
        SUCCESS = 'SUCCESS', 'Success'
        FAILURE = 'FAILURE', 'Failure'
        DENIED = 'DENIED', 'Denied'

    chain = models.CharField(max_length=64)
    seq = models.BigIntegerField()
    event_type = models.CharField(max_length=64, db_index=True)
    actor_id = models.BigIntegerField(null=True, blank=True)
    actor_label = models.CharField(max_length=150, blank=True)
    organization_id = models.BigIntegerField(null=True, blank=True)
    election_id = models.BigIntegerField(null=True, blank=True)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    summary = models.CharField(max_length=500, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    correlation_id = models.CharField(max_length=64, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    result = models.CharField(max_length=10, choices=Result.choices, default=Result.SUCCESS)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    prev_hash = models.CharField(max_length=64)
    hash = models.CharField(max_length=64, unique=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at', '-seq']
        constraints = [models.UniqueConstraint(fields=['chain', 'seq'], name='uniq_audit_chain_seq')]
        indexes = [
            models.Index(fields=['election_id', 'created_at']),
            models.Index(fields=['organization_id', 'created_at']),
            models.Index(fields=['actor_id', 'created_at']),
        ]

    def __str__(self):
        return f'{self.event_type} by {self.actor_label or "system"}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionDenied('Audit events are append-only.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied('Audit events are append-only.')


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
class IdempotencyRecord(models.Model):
    class State(models.TextChoices):
        IN_PROGRESS = 'IN_PROGRESS', 'In progress'
        COMPLETED = 'COMPLETED', 'Completed'

    scope = models.CharField(max_length=120)
    key = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    state = models.CharField(max_length=12, choices=State.choices, default=State.IN_PROGRESS)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['scope', 'key'], name='uniq_idempotency_scope_key')]


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------
class DataKey(models.Model):
    purpose = models.CharField(max_length=40, default='default')
    kek_id = models.CharField(max_length=300)
    wrapped_key = models.TextField()
    provider = models.CharField(max_length=20, default='local')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'DataKey {self.pk} ({self.purpose}, kek={self.kek_id}, active={self.is_active})'


# ---------------------------------------------------------------------------
# Staff/organizer account security
# ---------------------------------------------------------------------------
class UserSecurity(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='security')
    totp_secret = EncryptedTextField(blank=True, null=True)
    totp_confirmed_at = models.DateTimeField(null=True, blank=True)
    recovery_codes = models.JSONField(default=list, blank=True)
    failed_login_count = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_failed_at = models.DateTimeField(null=True, blank=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    mfa_enforced = models.BooleanField(default=False)

    @property
    def totp_enabled(self):
        return bool(self.totp_secret and self.totp_confirmed_at)

    @property
    def mfa_enabled(self):
        return self.totp_enabled or self.user.webauthn_credentials.exists()

    @property
    def is_locked(self):
        return bool(self.locked_until and self.locked_until > timezone.now())


class WebAuthnCredential(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='webauthn_credentials')
    credential_id = models.CharField(max_length=512, unique=True)
    public_key = models.TextField()
    sign_count = models.PositiveBigIntegerField(default=0)
    transports = models.JSONField(default=list, blank=True)
    name = models.CharField(max_length=80, default='Passkey')
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)


class UserSession(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='tracked_sessions')
    session_key = models.CharField(max_length=64, unique=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    device_hash = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    revoked = models.BooleanField(default=False)

    class Meta:
        ordering = ['-last_seen_at']


class KnownDevice(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='known_devices')
    device_hash = models.CharField(max_length=64)
    label = models.CharField(max_length=200, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'device_hash'], name='uniq_known_device')]


class ApiToken(models.Model):
    """Personal access token for the REST API. Only a hash is stored."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='api_tokens')
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True, related_name='api_tokens')
    name = models.CharField(max_length=80)
    prefix = models.CharField(max_length=12, db_index=True)
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_valid(self):
        if self.revoked_at:
            return False
        return not (self.expires_at and self.expires_at <= timezone.now())


class OTPChallenge(models.Model):
    """One-time passcode sent by email/SMS (voter login, verification, step-up)."""

    class Channel(models.TextChoices):
        EMAIL = 'EMAIL', 'Email'
        SMS = 'SMS', 'SMS'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    purpose = models.CharField(max_length=40)
    subject_type = models.CharField(max_length=40)
    subject_id = models.CharField(max_length=64)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    destination_hint = models.CharField(max_length=120, blank=True)
    code_hash = models.CharField(max_length=64)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['subject_type', 'subject_id', 'purpose'])]


# ---------------------------------------------------------------------------
# Support desk
# ---------------------------------------------------------------------------
class SupportTicket(models.Model):
    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        IN_PROGRESS = 'IN_PROGRESS', 'In progress'
        WAITING = 'WAITING', 'Waiting on requester'
        RESOLVED = 'RESOLVED', 'Resolved'
        CLOSED = 'CLOSED', 'Closed'

    class Priority(models.TextChoices):
        LOW = 'LOW', 'Low'
        NORMAL = 'NORMAL', 'Normal'
        HIGH = 'HIGH', 'High'
        URGENT = 'URGENT', 'Urgent'

    reference = models.CharField(max_length=16, unique=True, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.SET_NULL, null=True, blank=True, related_name='support_tickets')
    event = models.ForeignKey('voting.Event', on_delete=models.SET_NULL, null=True, blank=True, related_name='support_tickets')
    requester_name = models.CharField(max_length=150)
    requester_email = models.EmailField()
    subject = models.CharField(max_length=200)
    category = models.CharField(max_length=40, default='general')
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.NORMAL)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_tickets')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.reference} {self.subject}'

    def save(self, *args, **kwargs):
        if not self.reference:
            from .crypto import access_code
            self.reference = f'SUP-{access_code(8)}'
        super().save(*args, **kwargs)


class SupportMessage(models.Model):
    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name='messages')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    body = models.TextField()
    is_internal = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
