import hashlib
import hmac
import secrets
import string

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.timezone import now


def validate_file_size(file):
    # Limit to 2MB
    limit = 2 * 1024 * 1024
    if file.size > limit:
        raise ValidationError('File too large. Size should not exceed 2 MB.')


def generate_voting_code():
    """Kept for historical migrations; new credentials use core.crypto.access_code."""
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(8))


def hash_voting_code(event_id, code):
    """HMAC-SHA256 of a voter access code, keyed by SECRET_KEY and scoped to
    the election. Only this digest is ever used to look a credential up."""
    normalized = (code or '').strip().upper()
    message = f"{event_id}:{normalized}".encode('utf-8')
    return hmac.new(settings.SECRET_KEY.encode('utf-8'), message, hashlib.sha256).hexdigest()


def default_auth_methods():
    return ['CODE']


class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    is_approved_organizer = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.user.username} Profile"


class Event(models.Model):
    """An election: either paid public voting (reality shows, awards) or an
    institutional secret-ballot election."""

    class VotingMode(models.TextChoices):
        PAY_TO_VOTE = 'Pay to Vote', 'Paid public voting'
        CODE_VOTING = 'Code Voting', 'Institutional election (secret ballot)'

    class CodeVotingMode(models.TextChoices):
        STANDARD = 'Standard', 'Access code only'
        STUDENT_ID = 'Student ID', 'Voter ID + access code'

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        REVIEW = 'REVIEW', 'In review'
        APPROVED = 'APPROVED', 'Approved'
        SCHEDULED = 'SCHEDULED', 'Scheduled'
        OPEN = 'OPEN', 'Open'
        PAUSED = 'PAUSED', 'Paused'
        CLOSED = 'CLOSED', 'Closed'
        TALLYING = 'TALLYING', 'Tallying'
        CERTIFIED = 'CERTIFIED', 'Certified'
        PUBLISHED = 'PUBLISHED', 'Results published'
        ARCHIVED = 'ARCHIVED', 'Archived'

    class ResultsVisibility(models.TextChoices):
        LIVE = 'LIVE', 'Live counts while voting is open'
        AFTER_CLOSE = 'AFTER_CLOSE', 'Hidden until voting closes'
        AFTER_PUBLISH = 'AFTER_PUBLISH', 'Hidden until results are certified and published'

    class KeyCustody(models.TextChoices):
        SYSTEM = 'SYSTEM', 'Platform key management (KMS/KEK)'
        TRUSTEES = 'TRUSTEES', 'Split between election trustees (k-of-n)'

    AUTH_METHOD_CHOICES = [
        ('CODE', 'Voter ID and/or access code'),
        ('EMAIL_OTP', 'One-time code by email'),
        ('SMS_OTP', 'One-time code by SMS'),
        ('SSO', 'Institutional single sign-on (OIDC)'),
        ('LDAP', 'LDAP / Active Directory'),
        ('ACCOUNT', 'Platform account'),
    ]

    PUBLIC_STATUSES = (Status.SCHEDULED, Status.OPEN, Status.PAUSED, Status.CLOSED, Status.TALLYING,
                       Status.CERTIFIED, Status.PUBLISHED)

    organization = models.ForeignKey('core.Organization', on_delete=models.PROTECT, null=True, blank=True,
                                     related_name='events')
    voting_mode = models.CharField(max_length=20, choices=VotingMode.choices, default=VotingMode.PAY_TO_VOTE)
    code_voting_mode = models.CharField(max_length=20, choices=CodeVotingMode.choices, default=CodeVotingMode.STANDARD)
    enable_tie_breaker = models.BooleanField(default=False)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    timezone = models.CharField(max_length=64, default='Africa/Accra')
    currency = models.CharField(max_length=3, default='GHS')
    # Public listing toggle (independent of the lifecycle status).
    is_active = models.BooleanField(default=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    results_visibility = models.CharField(max_length=16, choices=ResultsVisibility.choices,
                                          default=ResultsVisibility.LIVE)

    # Voter authentication / registration (institutional elections)
    auth_methods = models.JSONField(default=default_auth_methods, blank=True)
    require_second_factor = models.BooleanField(default=False)
    allow_self_registration = models.BooleanField(default=False)
    registration_email_domains = models.CharField(max_length=300, blank=True,
                                                  help_text='Comma-separated, e.g. st.ug.edu.gh')

    # Integrity controls
    dual_approval_required = models.BooleanField(default=False)
    config_frozen = models.BooleanField(default=False)
    ballot_frozen = models.BooleanField(default=False)
    candidates_frozen = models.BooleanField(default=False)
    voter_list_frozen = models.BooleanField(default=False)
    legal_hold = models.BooleanField(default=False)
    submitted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    # Ballot secrecy / crypto
    record_constituency_on_ballot = models.BooleanField(default=False)
    min_anonymity_set = models.PositiveSmallIntegerField(default=5)
    key_custody = models.CharField(max_length=10, choices=KeyCustody.choices, default=KeyCustody.SYSTEM)
    trustee_threshold = models.PositiveSmallIntegerField(default=0)

    # Paid voting limits
    max_votes_per_voter = models.PositiveIntegerField(null=True, blank=True,
                                                      help_text='Per payer (email/phone/card) for the whole election.')
    min_votes_per_transaction = models.PositiveIntegerField(default=1)
    max_votes_per_transaction = models.PositiveIntegerField(null=True, blank=True)
    max_spend_per_voter = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    payment_channels = models.JSONField(default=list, blank=True)
    allowed_countries = models.JSONField(default=list, blank=True)

    # Theme Fields
    primary_color = models.CharField(max_length=7, default='#800020')
    accent_color = models.CharField(max_length=7, default='#FFD700')
    background_image = models.ImageField(upload_to='event_backgrounds/', blank=True, null=True, validators=[validate_file_size])
    event_image = models.ImageField(upload_to='event_flyers/', blank=True, null=True, validators=[validate_file_size])

    # Revenue Split Field
    platform_fee_percentage = models.DecimalField(max_digits=4, decimal_places=2, default=20.00)
    vote_price = models.DecimalField(max_digits=10, decimal_places=2, default=1.00)

    organizer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='events')

    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(auto_now=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    certified_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['status', 'start_date']), models.Index(fields=['status', 'end_date'])]

    def __str__(self):
        return self.title

    # -- convenience ---------------------------------------------------------
    @property
    def is_paid(self):
        return self.voting_mode == self.VotingMode.PAY_TO_VOTE

    @property
    def is_institutional(self):
        return self.voting_mode == self.VotingMode.CODE_VOTING

    @property
    def is_approved(self):
        return self.status not in (self.Status.DRAFT, self.Status.REVIEW)

    @property
    def voting_locked(self):
        return self.status == self.Status.PAUSED

    @property
    def is_public(self):
        return self.is_active and self.status in self.PUBLIC_STATUSES

    def accepting_votes(self, now=None):
        now = now or timezone.now()
        return self.status == self.Status.OPEN and self.start_date <= now < self.end_date

    @property
    def results_are_public(self):
        if self.status in (self.Status.PUBLISHED, self.Status.ARCHIVED):
            return True
        if self.results_visibility == self.ResultsVisibility.LIVE:
            return self.status in self.PUBLIC_STATUSES and self.is_paid
        if self.results_visibility == self.ResultsVisibility.AFTER_CLOSE:
            return self.status in (self.Status.CLOSED, self.Status.TALLYING, self.Status.CERTIFIED) and self.is_paid
        return False

    @property
    def email_domain_list(self):
        return [d.strip().lower().lstrip('@') for d in self.registration_email_domains.split(',') if d.strip()]

    def get_total_revenue(self):
        total = VoteTransaction.objects.filter(candidate__event=self, status=VoteTransaction.Status.SUCCESS) \
            .aggregate(total=Sum('amount'))['total']
        return total if total else 0

    def get_organizer_payout(self):
        total_revenue = self.get_total_revenue()
        fee = total_revenue * (self.platform_fee_percentage / 100)
        return total_revenue - fee


class Category(models.Model):
    """An election position / race / award category."""

    class BallotType(models.TextChoices):
        SINGLE = 'SINGLE', 'Single choice'
        FPTP = 'FPTP', 'First-past-the-post (plurality, multi-seat capable)'
        MULTIPLE = 'MULTIPLE', 'Multiple choice (choose up to N)'
        APPROVAL = 'APPROVAL', 'Approval voting'
        RANKED = 'RANKED', 'Ranked choice (IRV / STV)'
        SCORE = 'SCORE', 'Score / custom scoring'
        REFERENDUM = 'REFERENDUM', 'Yes / No referendum'

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='categories')
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    ballot_type = models.CharField(max_length=12, choices=BallotType.choices, default=BallotType.SINGLE)
    # Selection rules. For RANKED, max_select is the number of ranks allowed.
    min_select = models.PositiveSmallIntegerField(default=1)
    max_select = models.PositiveSmallIntegerField(default=1)
    allow_abstain = models.BooleanField(default=True)
    seats = models.PositiveSmallIntegerField(default=1)
    max_score = models.PositiveSmallIntegerField(default=10)
    referendum_threshold = models.DecimalField(max_digits=5, decimal_places=2, default=50,
                                               help_text='Percent of valid votes that must be YES (strictly more).')
    # Restricts who may vote for this position (voter's constituency must be
    # this one or a descendant). Empty = every eligible voter.
    constituency = models.ForeignKey('elections.Constituency', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='positions')
    display_order = models.PositiveIntegerField(default=0)
    # Paid voting: campaign-specific price and per-voter cap.
    vote_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    max_votes_per_voter = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ['display_order', 'id']

    def __str__(self):
        return self.name

    @property
    def is_referendum(self):
        return self.ballot_type == self.BallotType.REFERENDUM


class Candidate(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Active'
        WITHDRAWN = 'WITHDRAWN', 'Withdrawn'
        DISQUALIFIED = 'DISQUALIFIED', 'Disqualified'
        ELIMINATED = 'ELIMINATED', 'Eliminated'

    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='candidates', null=True, blank=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='candidates')
    name = models.CharField(max_length=100)
    bio = models.TextField(blank=True)
    manifesto = models.TextField(blank=True)
    affiliation = models.CharField(max_length=120, blank=True)
    nominee_code = models.CharField(max_length=10, unique=True, null=True, blank=True)
    image = models.ImageField(upload_to='candidate_images/', blank=True, null=True, validators=[validate_file_size])
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    display_order = models.PositiveIntegerField(default=0)
    email = models.EmailField(blank=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='candidacies')
    max_votes_per_voter = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['display_order', 'id']

    def __str__(self):
        return self.name

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    def save(self, *args, **kwargs):
        if not self.nominee_code:
            for _ in range(20):
                letters = ''.join(secrets.choice(string.ascii_uppercase) for _ in range(2))
                numbers = ''.join(secrets.choice(string.digits) for _ in range(3))
                code = f"{letters}{numbers}"
                if not Candidate.objects.filter(nominee_code=code).exists():
                    self.nominee_code = code
                    break
        super().save(*args, **kwargs)


class VoteTransaction(models.Model):
    """Ledger of paid (and ticket tie-breaker) votes. Institutional secret
    ballots never touch this table - they live in elections.Ballot."""

    class Status(models.TextChoices):
        PENDING = 'Pending', 'Pending'
        SUCCESS = 'Success', 'Success'
        FAILED = 'Failed', 'Failed'
        REVERSED = 'Reversed', 'Reversed (refund/chargeback)'

    class VoteType(models.TextChoices):
        MAIN = 'Main', 'Main Vote'
        TIE_BREAKER = 'Tie-Breaker', 'Tie-Breaker Vote'

    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name='transactions')
    payment = models.OneToOneField('payments.Payment', on_delete=models.PROTECT, null=True, blank=True,
                                   related_name='vote_transaction')
    voter_email = models.EmailField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    paystack_reference = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    vote_type = models.CharField(max_length=20, choices=VoteType.choices, default=VoteType.MAIN)
    number_of_votes = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['candidate', 'status', 'vote_type'])]

    def __str__(self):
        return f"{self.voter_email} - {self.candidate.name} - {self.status}"


class ProductCategory(models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Product(models.Model):
    category = models.ForeignKey(ProductCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='products')
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    old_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    image = models.ImageField(upload_to='product_images/', blank=True, null=True, validators=[validate_file_size])
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    @property
    def discount_percentage(self):
        if self.old_price and self.old_price > self.price:
            discount = ((self.old_price - self.price) / self.old_price) * 100
            return int(discount)
        return 0


class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='product_images/', validators=[validate_file_size])

    def __str__(self):
        return f"Image for {self.product.name}"


class Ticket(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='tickets')
    name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    old_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    quantity_available = models.PositiveIntegerField(default=100)
    image = models.ImageField(upload_to='ticket_images/', blank=True, null=True, validators=[validate_file_size])
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} - {self.event.title}"

    @property
    def discount_percentage(self):
        if self.old_price and self.old_price > self.price:
            discount = ((self.old_price - self.price) / self.old_price) * 100
            return int(discount)
        return 0

    @property
    def sold_count(self):
        total = self.purchases.filter(status='Success').aggregate(total=Sum('quantity'))['total']
        return total if total else 0

    @property
    def remaining(self):
        return self.quantity_available - self.sold_count

    def reserved_count(self):
        """Sold plus still-pending purchases (used to stop overselling)."""
        total = self.purchases.filter(Q(status='Success') | Q(status='Pending')).aggregate(total=Sum('quantity'))['total']
        return total or 0


class TicketPurchase(models.Model):
    class PurchaseMethod(models.TextChoices):
        WEB = 'Web', 'Web'
        USSD = 'USSD', 'USSD'

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='purchases')
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='ticket_purchases')
    buyer_name = models.CharField(max_length=150, blank=True, null=True)
    buyer_email = models.EmailField()
    quantity = models.PositiveIntegerField(default=1)
    paystack_reference = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=10, default='Pending')
    purchase_method = models.CharField(max_length=10, choices=PurchaseMethod.choices, default=PurchaseMethod.WEB)
    is_checked_in = models.BooleanField(default=False)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    has_voted = models.BooleanField(default=False)
    purchased_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.buyer_name} - {self.ticket.name}"

    @property
    def expected_amount(self):
        return self.ticket.price * self.quantity
