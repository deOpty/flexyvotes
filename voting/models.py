import uuid
import random
import string
from django.db import models
from django.contrib.auth.models import User
from django.db.models import Sum, Q, IntegerField
from django.db.models.functions import Coalesce
from django.core.exceptions import ValidationError
from django.db.models import Sum
import os

def validate_file_size(file):
    # Limit to 2MB
    limit = 2 * 1024 * 1024
    if file.size > limit:
        raise ValidationError('File too large. Size should not exceed 2 MB.')


def generate_voting_code():
    return uuid.uuid4().hex[:8].upper()

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


# This is for categories WITHIN the event (e.g., Best Male, Best Female)
class Category(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='categories')
    name = models.CharField(max_length=100)

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
    @property
    def discount_percentage(self):
        if self.old_price and self.old_price > self.price:
            discount = ((self.old_price - self.price) / self.old_price) * 100
            return int(discount)
        return 0
    
class VotingCode(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='voting_codes')
    # REMOVED unique=True from here
    code = models.CharField(max_length=50, default=uuid.uuid4().hex[:8].upper())
    voter_identifier = models.CharField(max_length=100, blank=True, null=True) 
    is_used = models.BooleanField(default=False)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # NEW: Enforce uniqueness only per event
    class Meta:
        unique_together = ('event', 'code')

    def __str__(self):
        if self.voter_identifier:
            return f"{self.code} - {self.voter_identifier} - {'Used' if self.is_used else 'Valid'}"
        return f"{self.code} - {'Used' if self.is_used else 'Valid'}"

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
