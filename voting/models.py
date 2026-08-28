import uuid
import random
import secrets
import string
import hmac
import hashlib
from django.conf import settings
from django.db import models
from django.contrib.auth.models import User
from django.db.models import Sum, Q, IntegerField
from django.db.models.functions import Coalesce
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils import timezone
import os

def validate_file_size(file):
    # Limit to 2MB
    limit = 2 * 1024 * 1024
    if file.size > limit:
        raise ValidationError('File too large. Size should not exceed 2 MB.')


def generate_voting_code():
    # secrets (not uuid4/random) is the intention-revealing CSPRNG choice for
    # bearer credentials like this.
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(8))


def hash_voting_code(event_id, code):
    """HMAC-SHA256 the code, keyed by SECRET_KEY, scoped to the event.

    Voting codes are bearer secrets - only a salted/keyed digest is ever
    persisted, never the plaintext, so a database dump alone can't be used
    to cast votes. Scoping the HMAC input to event_id keeps the same code
    string in two different events from colliding in the hash index (the
    uniqueness constraint is per-event, not global).
    """
    normalized = (code or '').strip().upper()
    message = f"{event_id}:{normalized}".encode('utf-8')
    return hmac.new(settings.SECRET_KEY.encode('utf-8'), message, hashlib.sha256).hexdigest()

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    is_approved_organizer = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.user.username} Profile"

class Event(models.Model):
    class VotingMode(models.TextChoices):
        PAY_TO_VOTE = 'Pay to Vote', 'Pay to Vote'
        CODE_VOTING = 'Code Voting', 'Code Voting'

    voting_mode = models.CharField(max_length=20, choices=VotingMode.choices, default=VotingMode.PAY_TO_VOTE)
    
    class CodeVotingMode(models.TextChoices):
        STANDARD = 'Standard', 'Standard Codes'
        STUDENT_ID = 'Student ID', 'Student ID + Code'
        
    code_voting_mode = models.CharField(max_length=20, choices=CodeVotingMode.choices, default=CodeVotingMode.STANDARD)
     
    # NEW: Tie-Breaker Toggle
    enable_tie_breaker = models.BooleanField(default=False) # <--- ADD THIS LINE
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    is_approved = models.BooleanField(default=False) # <--- ADD THIS
    # Explicit organizer/admin kill-switch for voting, independent of the
    # scheduled start_date/end_date window - lets officials close an
    # election immediately (e.g. to investigate an issue) without editing
    # the schedule.
    voting_locked = models.BooleanField(default=False)
    
    # Theme Fields
    primary_color = models.CharField(max_length=7, default='#800020') 
    accent_color = models.CharField(max_length=7, default='#FFD700')  
    background_image = models.ImageField(upload_to='event_backgrounds/', blank=True, null=True, validators=[validate_file_size])
    event_image = models.ImageField(upload_to='event_flyers/', blank=True, null=True, validators=[validate_file_size])

    # Revenue Split Field
    platform_fee_percentage = models.DecimalField(max_digits=4, decimal_places=2, default=20.00)
    vote_price = models.DecimalField(max_digits=10, decimal_places=2, default=1.00) # <--- ADD THIS
    
    # Organizer Field
    organizer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='events')

    def __str__(self):
        return self.title

    def get_total_revenue(self):
        total = self.candidates.aggregate(
            total=Sum('transactions__amount', filter=Q(transactions__status='Success'))
        )['total']
        return total if total else 0

    def get_organizer_payout(self):
        total_revenue = self.get_total_revenue()
        fee = total_revenue * (self.platform_fee_percentage / 100)
        return total_revenue - fee


# This is for categories WITHIN the event (e.g., Best Male, Best Female) -
# i.e. an election "position".
class Category(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='categories')
    name = models.CharField(max_length=100)
    # Ballot rules for this position. max_select > 1 makes it a multi-choice
    # (checkbox) position instead of single-choice (radio).
    min_select = models.PositiveSmallIntegerField(default=1)
    max_select = models.PositiveSmallIntegerField(default=1)
    allow_abstain = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Candidate(models.Model):
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='candidates', null=True, blank=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='candidates')
    name = models.CharField(max_length=100)
    bio = models.TextField(blank=True)
    nominee_code = models.CharField(max_length=10, unique=True, null=True, blank=True)
    image = models.ImageField(upload_to='candidate_images/', blank=True, null=True, validators=[validate_file_size])

    def __str__(self):
        return self.name

    # NEW: Auto-generate nominee code if left blank
    def save(self, *args, **kwargs):
        if not self.nominee_code:
            # Generate a code like "TE025"
            is_unique = False
            while not is_unique:
                letters = ''.join(random.choices(string.ascii_uppercase, k=2))
                numbers = ''.join(random.choices(string.digits, k=3))
                generated_code = f"{letters}{numbers}"
                
                # Check if it already exists in the database
                if not Candidate.objects.filter(nominee_code=generated_code).exists():
                    self.nominee_code = generated_code
                    is_unique = True
                    
        super().save(*args, **kwargs)

class VoteTransaction(models.Model):
    class Status(models.TextChoices):
        PENDING = 'Pending', 'Pending'
        SUCCESS = 'Success', 'Success'
        FAILED = 'Failed', 'Failed'

    # NEW: Vote Type
    class VoteType(models.TextChoices):
        MAIN = 'Main', 'Main Vote'
        TIE_BREAKER = 'Tie-Breaker', 'Tie-Breaker Vote'

    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name='transactions')
    voter_email = models.EmailField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    paystack_reference = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    vote_type = models.CharField(max_length=20, choices=VoteType.choices, default=VoteType.MAIN)
    number_of_votes = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.voter_email} - {self.candidate.name} - {self.status}"

    
class ActivityLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    event = models.ForeignKey(Event, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} - {self.action}"


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
    image = models.ImageField(upload_to='product_images/', blank=True, null=True, validators=[validate_file_size]) # Keep as main thumbnail
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

# NEW: Model for multiple product images
class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='product_images/', validators=[validate_file_size])

    def __str__(self):
        return f"Image for {self.product.name}"

class VotingCode(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='voting_codes')
    # Uniqueness is enforced per-event via Meta.unique_together below, not
    # globally on this field. The default MUST be a callable (not a
    # pre-computed value) - a plain value here is evaluated exactly once at
    # class-definition time, so every VotingCode created without an explicit
    # code would silently get the *same* value, colliding on the very next
    # save for the same event.
    code = models.CharField(max_length=50, default=generate_voting_code)
    # SECURITY: this is the field every lookup/verification path actually
    # queries on (see views.validate_ballot_code/cast_ballot). `code` above
    # is kept only so a freshly-generated, still-unused code can be shown to
    # the organizer once (CSV export); the live credential check never
    # trusts it directly, so a DB dump alone can't be replayed as a vote.
    code_hash = models.CharField(max_length=64, db_index=True, blank=True)
    voter_identifier = models.CharField(max_length=100, blank=True, null=True)
    # Roster email, captured at import time. retrieve_voting_code() only
    # ever sends a reset code to this stored address - never to whatever
    # email a requester types into the public form - so knowing someone
    # else's student ID isn't enough to steal their credential.
    voter_email = models.EmailField(blank=True, null=True)
    is_used = models.BooleanField(default=False)
    used_at = models.DateTimeField(null=True, blank=True)
    invalidated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # NEW: Enforce uniqueness only per event
    class Meta:
        unique_together = ('event', 'code_hash')

    def __str__(self):
        if self.voter_identifier:
            return f"{self.code} - {self.voter_identifier} - {'Used' if self.is_used else 'Valid'}"
        return f"{self.code} - {'Used' if self.is_used else 'Valid'}"

    def save(self, *args, **kwargs):
        if self.event_id and self.code:
            self.code_hash = hash_voting_code(self.event_id, self.code)
        super().save(*args, **kwargs)

    def mark_used(self):
        # Scrub the plaintext code once spent - code_hash (unaffected, since
        # save() only recomputes it from a non-blank `code`) is all that's
        # needed afterward to know this credential was consumed.
        self.is_used = True
        self.used_at = timezone.now()
        self.code = ''
        self.save(update_fields=['is_used', 'used_at', 'code'])

    def reset(self):
        """Invalidate this code and issue a brand-new replacement.

        Used instead of ever re-displaying a previously-generated code:
        credentials are shown once and, if lost, replaced rather than
        recovered from storage.
        """
        self.is_used = True
        self.invalidated_at = timezone.now()
        self.code = ''
        self.save(update_fields=['is_used', 'invalidated_at', 'code'])
        return VotingCode.objects.create(
            event=self.event,
            code=generate_voting_code(),
            voter_identifier=self.voter_identifier,
            voter_email=self.voter_email,
        )

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

    # NEW: Calculate how many have been sold
    @property
    def sold_count(self):
        total = self.purchases.filter(status='Success').aggregate(total=Sum('quantity'))['total']
        return total if total else 0

    # NEW: Calculate how many are left
    @property
    def remaining(self):
        return self.quantity_available - self.sold_count

class TicketPurchase(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='purchases')
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='ticket_purchases')
    buyer_name = models.CharField(max_length=150, blank=True, null=True)
    buyer_email = models.EmailField()
    quantity = models.PositiveIntegerField(default=1)
    paystack_reference = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=10, default='Pending')
    
    # NEW: Track if ticket was bought on Web or USSD
    class PurchaseMethod(models.TextChoices):
        WEB = 'Web', 'Web'
        USSD = 'USSD', 'USSD'
        
    purchase_method = models.CharField(max_length=10, choices=PurchaseMethod.choices, default=PurchaseMethod.WEB) # <--- ADD THIS
    is_checked_in = models.BooleanField(default=False) 
    checked_in_at = models.DateTimeField(null=True, blank=True)
    has_voted = models.BooleanField(default=False) 
    purchased_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.buyer_name} - {self.ticket.name}"
