import uuid

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.crypto import access_code, blind_index
from core.fields import EncryptedTextField
from core.models import AppendOnlyQuerySet
from core.storage import private_storage


def _reference(prefix):
    return f'{prefix}-{access_code(8)}'


class Constituency(models.Model):
    """A node in an organization's structure (faculty, department, region...)."""

    class Kind(models.TextChoices):
        ROOT = 'ROOT', 'Whole organization'
        COLLEGE = 'COLLEGE', 'College'
        FACULTY = 'FACULTY', 'Faculty / school'
        DEPARTMENT = 'DEPARTMENT', 'Department'
        HALL = 'HALL', 'Hall / residence'
        HEADQUARTERS = 'HEADQUARTERS', 'Headquarters'
        REGION = 'REGION', 'Region'
        BRANCH = 'BRANCH', 'Branch / chapter'
        OTHER = 'OTHER', 'Other'

    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, related_name='constituencies')
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=50)
    kind = models.CharField(max_length=15, choices=Kind.choices, default=Kind.OTHER)
    # Materialized path of ancestor ids ("3/17/42/") for subtree queries.
    path = models.CharField(max_length=500, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['path']
        constraints = [models.UniqueConstraint(fields=['organization', 'code'], name='uniq_constituency_code')]
        verbose_name_plural = 'constituencies'

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.parent_id and self.parent.organization_id != self.organization_id:
            raise ValueError('Parent constituency belongs to a different organization.')
        super().save(*args, **kwargs)
        path = f'{self.parent.path if self.parent_id else ""}{self.pk}/'
        if path != self.path:
            old = self.path
            Constituency.objects.filter(pk=self.pk).update(path=path)
            self.path = path
            if old:
                for child in Constituency.objects.filter(path__startswith=old).exclude(pk=self.pk):
                    Constituency.objects.filter(pk=child.pk).update(path=path + child.path[len(old):])

    @property
    def depth(self):
        return self.path.count('/') - 1

    def is_within(self, ancestor):
        return bool(ancestor and self.path.startswith(ancestor.path))

    def subtree(self):
        return Constituency.objects.filter(path__startswith=self.path)


class Voter(models.Model):
    """An entry on an election's voter roll.

    Holds identity and eligibility only - it is never linked to a ballot.
    Name, email and phone are encrypted at rest; *_index columns are blind
    indexes for equality search.
    """

    class Status(models.TextChoices):
        ELIGIBLE = 'ELIGIBLE', 'Eligible'
        VERIFIED = 'VERIFIED', 'Verified'
        VOTED = 'VOTED', 'Voted'
        SUSPENDED = 'SUSPENDED', 'Suspended'
        INELIGIBLE = 'INELIGIBLE', 'Ineligible'

    class Source(models.TextChoices):
        MANUAL = 'MANUAL', 'Manual entry'
        CSV = 'CSV', 'CSV import'
        XLSX = 'XLSX', 'Excel import'
        API = 'API', 'API'
        SELF_REGISTRATION = 'SELF_REGISTRATION', 'Self registration'
        SSO = 'SSO', 'Single sign-on'
        CODES = 'CODES', 'Anonymous access codes'
        LEGACY = 'LEGACY', 'Migrated'

    election = models.ForeignKey('voting.Event', on_delete=models.CASCADE, related_name='voters')
    identifier = models.CharField(max_length=100, null=True, blank=True)
    full_name = EncryptedTextField(blank=True, default='')
    email = EncryptedTextField(blank=True, null=True)
    email_index = models.CharField(max_length=64, blank=True, db_index=True)
    phone = EncryptedTextField(blank=True, null=True)
    phone_index = models.CharField(max_length=64, blank=True, db_index=True)
    constituency = models.ForeignKey(Constituency, on_delete=models.SET_NULL, null=True, blank=True, related_name='voters')
    attributes = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ELIGIBLE)
    status_reason = models.CharField(max_length=255, blank=True)
    credential_hash = models.CharField(max_length=64, null=True, blank=True)
    # The plaintext access code, envelope-encrypted, kept only until it is
    # used so officials can distribute it; wiped once the voter votes.
    credential_ciphertext = EncryptedTextField(null=True, blank=True)
    credential_issued_at = models.DateTimeField(null=True, blank=True)
    credential_version = models.PositiveSmallIntegerField(default=0)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    phone_verified_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='voter_records')
    sso_subject_index = models.CharField(max_length=64, blank=True, db_index=True)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    invited_at = models.DateTimeField(null=True, blank=True)
    last_reminded_at = models.DateTimeField(null=True, blank=True)
    voted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['election', 'identifier'], condition=Q(identifier__isnull=False),
                                    name='uniq_voter_identifier'),
            models.UniqueConstraint(fields=['election', 'credential_hash'], condition=Q(credential_hash__isnull=False),
                                    name='uniq_voter_credential'),
        ]
        indexes = [models.Index(fields=['election', 'status'])]

    def __str__(self):
        return self.identifier or f'Voter #{self.pk}'

    @property
    def can_vote(self):
        return self.status in (self.Status.ELIGIBLE, self.Status.VERIFIED)

    def set_email(self, email):
        email = (email or '').strip() or None
        self.email = email
        self.email_index = blind_index(email, 'email') if email else ''

    def set_phone(self, phone):
        from core.utils import normalize_phone

        phone = normalize_phone(phone) or None
        self.phone = phone
        self.phone_index = blind_index(phone, 'phone') if phone else ''

    def set_credential(self, code):
        from voting.models import hash_voting_code

        self.credential_hash = hash_voting_code(self.election_id, code)
        self.credential_ciphertext = code.strip().upper()
        self.credential_issued_at = timezone.now()
        self.credential_version += 1

    def issue_credential(self):
        code = access_code(10)
        self.set_credential(code)
        return code

    def wipe_credential_plaintext(self):
        self.credential_ciphertext = None


class EligibilityRule(models.Model):
    """Additional conditions a voter must meet, for the whole election or
    for a single position. All active rules in scope must pass."""

    class Kind(models.TextChoices):
        CONSTITUENCY = 'CONSTITUENCY', 'Belongs to constituency (or its sub-units)'
        ATTRIBUTE_EQUALS = 'ATTRIBUTE_EQUALS', 'Attribute equals value'
        ATTRIBUTE_IN = 'ATTRIBUTE_IN', 'Attribute is one of values'
        ATTRIBUTE_NOT_IN = 'ATTRIBUTE_NOT_IN', 'Attribute is not one of values'
        EMAIL_DOMAIN = 'EMAIL_DOMAIN', 'Email address domain is one of values'
        VERIFIED_EMAIL = 'VERIFIED_EMAIL', 'Email address verified'
        VERIFIED_PHONE = 'VERIFIED_PHONE', 'Phone number verified'

    election = models.ForeignKey('voting.Event', on_delete=models.CASCADE, related_name='eligibility_rules')
    position = models.ForeignKey('voting.Category', on_delete=models.CASCADE, null=True, blank=True,
                                 related_name='eligibility_rules')
    kind = models.CharField(max_length=20, choices=Kind.choices)
    attribute = models.CharField(max_length=60, blank=True)
    values = models.JSONField(default=list, blank=True)
    constituency = models.ForeignKey(Constituency, on_delete=models.CASCADE, null=True, blank=True)
    description = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        scope = self.position.name if self.position_id else 'whole election'
        return f'{self.get_kind_display()} ({scope})'


class VoteAuthorization(models.Model):
    """Permission for one voter to cast one ballot, bound to a random ballot
    token of which only the hash is stored. The ballot itself never refers
    back to this row."""

    class Status(models.TextChoices):
        ISSUED = 'ISSUED', 'Issued'
        CONSUMED = 'CONSUMED', 'Consumed'
        REVOKED = 'REVOKED', 'Revoked'
        EXPIRED = 'EXPIRED', 'Expired'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey('voting.Event', on_delete=models.PROTECT, related_name='vote_authorizations')
    voter = models.ForeignKey(Voter, on_delete=models.PROTECT, related_name='authorizations')
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ISSUED)
    auth_method = models.CharField(max_length=20)
    ballot_style = models.JSONField(default=list)
    issued_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['voter'], condition=Q(status='CONSUMED'),
                                    name='one_consumed_authorization_per_voter'),
        ]


class ElectionKey(models.Model):
    """The election's ballot-encryption key pair."""

    election = models.OneToOneField('voting.Event', on_delete=models.PROTECT, related_name='ballot_key')
    public_key = models.CharField(max_length=64)
    fingerprint = models.CharField(max_length=16)
    custody = models.CharField(max_length=10, default='SYSTEM')
    # System custody: the raw private key, envelope-encrypted.
    wrapped_private_key = EncryptedTextField(null=True, blank=True)
    threshold = models.PositiveSmallIntegerField(default=0)
    shares = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class TrusteeShare(models.Model):
    """One trustee's Shamir share of the election private key."""

    election = models.ForeignKey('voting.Event', on_delete=models.CASCADE, related_name='trustee_shares')
    trustee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='trustee_shares')
    index = models.PositiveSmallIntegerField()
    share_hash = models.CharField(max_length=64)
    # Held (encrypted) only until the trustee collects it.
    pending_share = EncryptedTextField(null=True, blank=True)
    collected_at = models.DateTimeField(null=True, blank=True)
    # Supplied back by the trustee for the tally; wiped afterwards.
    submitted_share = EncryptedTextField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['election', 'trustee'], name='uniq_trustee_per_election'),
            models.UniqueConstraint(fields=['election', 'index'], name='uniq_trustee_index'),
        ]


class Ballot(models.Model):
    """An anonymous, encrypted ballot. Random UUID primary key and no
    timestamp, voter reference or authorization reference, so neither
    insertion order nor time can be joined back to the voter roll."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey('voting.Event', on_delete=models.PROTECT, related_name='ballots')
    ciphertext = models.TextField()
    tracker = models.CharField(max_length=64, unique=True)
    style_hash = models.CharField(max_length=64)
    # Only populated when the election records constituency-level results.
    constituency_id = models.BigIntegerField(null=True, blank=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ['tracker']

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionDenied('Ballots are immutable.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied('Ballots are immutable.')


class ElectionConfigSnapshot(models.Model):
    """Signed snapshot of the election configuration at approval time."""

    election = models.ForeignKey('voting.Event', on_delete=models.CASCADE, related_name='config_snapshots')
    version = models.PositiveIntegerField()
    reason = models.CharField(max_length=40)
    config = models.JSONField()
    config_hash = models.CharField(max_length=64)
    signature = models.CharField(max_length=200)
    public_key = models.CharField(max_length=64)
    key_id = models.CharField(max_length=16)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-version']
        constraints = [models.UniqueConstraint(fields=['election', 'version'], name='uniq_config_snapshot_version')]


class ElectionResult(models.Model):
    class Kind(models.TextChoices):
        OFFICIAL = 'OFFICIAL', 'Official tally'
        RECOUNT = 'RECOUNT', 'Recount'
        INDEPENDENT = 'INDEPENDENT', 'Independent (auditor) recount'

    class Status(models.TextChoices):
        PENDING_REVIEW = 'PENDING_REVIEW', 'Awaiting results review'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        SUPERSEDED = 'SUPERSEDED', 'Superseded'
        INFORMATIONAL = 'INFORMATIONAL', 'Informational (recount)'

    election = models.ForeignKey('voting.Event', on_delete=models.CASCADE, related_name='results')
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.OFFICIAL)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING_REVIEW)
    data = models.JSONField()
    result_hash = models.CharField(max_length=64)
    bulletin_root = models.CharField(max_length=64, blank=True)
    ballots_counted = models.PositiveIntegerField(default=0)
    eligible_voters = models.PositiveIntegerField(default=0)
    votes_cast = models.PositiveIntegerField(default=0)
    tallied_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    tallied_at = models.DateTimeField(default=timezone.now)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-tallied_at']


class ResultCertification(models.Model):
    election = models.ForeignKey('voting.Event', on_delete=models.CASCADE, related_name='certifications')
    result = models.ForeignKey(ElectionResult, on_delete=models.PROTECT, related_name='certifications')
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=64)
    signature = models.CharField(max_length=200)
    public_key = models.CharField(max_length=64)
    key_id = models.CharField(max_length=16)
    tallied_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    certified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    certified_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.TextField(blank=True)

    class Meta:
        ordering = ['-certified_at']

    @property
    def is_current(self):
        return self.revoked_at is None


class ApprovalRequest(models.Model):
    """Dual-control: a sensitive action requested by one person and approved
    by a different person before it executes."""

    class Action(models.TextChoices):
        UNFREEZE = 'UNFREEZE', 'Lift a freeze'
        EXTEND_VOTING = 'EXTEND_VOTING', 'Extend the voting period'
        REOPEN_VOTING = 'REOPEN_VOTING', 'Re-open closed voting'
        BULK_CREDENTIAL_RESET = 'BULK_CREDENTIAL_RESET', 'Reset all unused voter credentials'
        DECERTIFY = 'DECERTIFY', 'Revoke result certification'
        RELEASE_LEGAL_HOLD = 'RELEASE_LEGAL_HOLD', 'Release a legal hold'
        REFUND = 'REFUND', 'Approve a large refund'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        EXECUTED = 'EXECUTED', 'Executed'
        FAILED = 'FAILED', 'Execution failed'
        CANCELLED = 'CANCELLED', 'Cancelled'
        EXPIRED = 'EXPIRED', 'Expired'

    election = models.ForeignKey('voting.Event', on_delete=models.CASCADE, null=True, blank=True,
                                 related_name='approval_requests')
    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, null=True, blank=True)
    action = models.CharField(max_length=24, choices=Action.choices)
    payload = models.JSONField(default=dict, blank=True)
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ['-requested_at']

    def __str__(self):
        return f'{self.get_action_display()} ({self.get_status_display()})'


class Recount(models.Model):
    class Kind(models.TextChoices):
        MANUAL = 'MANUAL', 'Manual recount'
        INDEPENDENT = 'INDEPENDENT', 'Independent recount'

    election = models.ForeignKey('voting.Event', on_delete=models.CASCADE, related_name='recounts')
    kind = models.CharField(max_length=12, choices=Kind.choices)
    reason = models.TextField(blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    result = models.ForeignKey(ElectionResult, on_delete=models.SET_NULL, null=True, blank=True)
    compared_to = models.ForeignKey(ElectionResult, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    matches = models.BooleanField(null=True)
    differences = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class Dispute(models.Model):
    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        UNDER_REVIEW = 'UNDER_REVIEW', 'Under review'
        UPHELD = 'UPHELD', 'Resolved - upheld'
        DISMISSED = 'DISMISSED', 'Resolved - dismissed'
        WITHDRAWN = 'WITHDRAWN', 'Withdrawn'

    class Category(models.TextChoices):
        ELIGIBILITY = 'ELIGIBILITY', 'Voter eligibility'
        CANDIDACY = 'CANDIDACY', 'Candidate eligibility'
        CONDUCT = 'CONDUCT', 'Election conduct'
        TECHNICAL = 'TECHNICAL', 'Technical problem'
        RESULT = 'RESULT', 'Result challenge'
        OTHER = 'OTHER', 'Other'

    reference = models.CharField(max_length=16, unique=True, editable=False)
    election = models.ForeignKey('voting.Event', on_delete=models.CASCADE, related_name='disputes')
    filed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    filer_name = models.CharField(max_length=150)
    filer_email = EncryptedTextField()
    filer_role = models.CharField(max_length=40, default='VOTER')
    category = models.CharField(max_length=12, choices=Category.choices, default=Category.OTHER)
    subject = models.CharField(max_length=200)
    description = models.TextField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    resolution = models.TextField(blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = _reference('DSP')
        super().save(*args, **kwargs)

    @property
    def is_open(self):
        return self.status in (self.Status.OPEN, self.Status.UNDER_REVIEW)


class Incident(models.Model):
    class Severity(models.TextChoices):
        LOW = 'LOW', 'Low'
        MEDIUM = 'MEDIUM', 'Medium'
        HIGH = 'HIGH', 'High'
        CRITICAL = 'CRITICAL', 'Critical'

    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        INVESTIGATING = 'INVESTIGATING', 'Investigating'
        MITIGATED = 'MITIGATED', 'Mitigated'
        RESOLVED = 'RESOLVED', 'Resolved'
        CLOSED = 'CLOSED', 'Closed'

    reference = models.CharField(max_length=16, unique=True, editable=False)
    election = models.ForeignKey('voting.Event', on_delete=models.CASCADE, null=True, blank=True, related_name='incidents')
    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, null=True, blank=True)
    title = models.CharField(max_length=200)
    description = models.TextField()
    impact = models.TextField(blank=True)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.MEDIUM)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.OPEN)
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = _reference('INC')
        super().save(*args, **kwargs)


class CaseNote(models.Model):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, null=True, blank=True, related_name='notes')
    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, null=True, blank=True, related_name='notes')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']


class EvidenceItem(models.Model):
    """Preserved evidence: private storage, content hash, never modified."""

    election = models.ForeignKey('voting.Event', on_delete=models.PROTECT, related_name='evidence')
    dispute = models.ForeignKey(Dispute, on_delete=models.PROTECT, null=True, blank=True, related_name='evidence')
    incident = models.ForeignKey(Incident, on_delete=models.PROTECT, null=True, blank=True, related_name='evidence')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    file = models.FileField(storage=private_storage, upload_to='evidence/%Y/%m/')
    sha256 = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField()
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ['-uploaded_at']

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionDenied('Evidence items are immutable.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied('Evidence items are immutable.')


class CandidateDocument(models.Model):
    candidate = models.ForeignKey('voting.Candidate', on_delete=models.CASCADE, related_name='documents')
    title = models.CharField(max_length=150)
    file = models.FileField(upload_to='candidate_documents/')
    sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']
