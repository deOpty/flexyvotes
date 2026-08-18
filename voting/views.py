import json
import hmac
import hashlib
import csv
import random
import string
import io
import uuid
import qrcode
import base64
from decimal import Decimal, InvalidOperation
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.core.cache import cache
from PIL import Image, ImageDraw, ImageFont
from django.core.mail import EmailMessage
from django.core.mail import send_mail
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from django.http import HttpResponse
from django.http import JsonResponse 
from django.contrib import messages
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db.models import Sum, Q, IntegerField
from django.db.models.functions import Coalesce
from django.views.decorators.csrf import csrf_exempt
from .models import (Event, Candidate, VoteTransaction, Profile, ActivityLog, 
                     Category, Product, ProductCategory, ProductImage, VotingCode, Ticket, TicketPurchase)
from .services import initialize_paystack_payment, initialize_ticket_payment, verify_paystack_transaction

# SECURITY: CSV Macro Injection Sanitizer
def sanitize_csv_value(value):
    if isinstance(value, str) and value and value[0] in ('=', '+', '-', '@'):
        return "'" + value
    return value

def parse_non_negative_decimal(raw_value, field_label, required=True):
    if raw_value is None or str(raw_value).strip() == '':
        if required:
            return None, f'{field_label} is required.'
        return None, None
    try:
        value = Decimal(str(raw_value))
    except (InvalidOperation, ValueError, TypeError):
        return None, f'{field_label} must be a valid number.'
    if value < 0:
        return None, f'{field_label} cannot be negative.'
    return value, None

def parse_non_negative_int(raw_value, field_label, required=True):
    if raw_value is None or str(raw_value).strip() == '':
        if required:
            return None, f'{field_label} is required.'
        return None, None
    try:
        value = int(raw_value)
    except (ValueError, TypeError):
        return None, f'{field_label} must be a whole number.'
    if value < 0:
        return None, f'{field_label} cannot be negative.'
    return value, None

def home(request):
    query = request.GET.get('q')
    if query:
        active_events = Event.objects.filter(is_active=True, is_approved=True, title__icontains=query).order_by('-start_date')
    else:
        active_events = Event.objects.filter(is_active=True, is_approved=True).order_by('-start_date')
        
    show_popup = request.session.pop('show_registration_popup', False)
    return render(request, 'voting/home.html', {'events': active_events, 'show_popup': show_popup})

def event_detail(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    is_expired = timezone.now() > event.end_date
    
    is_organizer_or_admin = False
    if request.user.is_authenticated:
        if request.user.is_staff or (hasattr(request.user, 'profile') and request.user.profile.is_approved_organizer):
            is_organizer_or_admin = True

    candidates = event.candidates.annotate(
        vote_count=Coalesce(Sum('transactions__number_of_votes', filter=Q(transactions__status='Success', transactions__vote_type='Main')), 0, output_field=IntegerField()),
        tie_breaker_count=Coalesce(Sum('transactions__number_of_votes', filter=Q(transactions__status='Success', transactions__vote_type='Tie-Breaker')), 0, output_field=IntegerField())
    ).order_by('-vote_count', '-tie_breaker_count', 'name')
    
    total_votes = sum(c.vote_count for c in candidates)
    for c in candidates:
        c.percentage = int((c.vote_count / total_votes) * 100) if total_votes > 0 else 0
    
    return render(request, 'voting/event_detail.html', {
        'event': event, 'candidates': candidates, 'is_expired': is_expired, 'is_organizer_or_admin': is_organizer_or_admin
    })

def initiate_vote(request, candidate_id):
    if request.user.is_authenticated:
        if request.user.is_staff or (hasattr(request.user, 'profile') and request.user.profile.is_approved_organizer):
            candidate = get_object_or_404(Candidate, id=candidate_id)
            messages.error(request, "Admins and Organizers cannot vote.")
            return redirect('event_detail', event_id=candidate.event.id)

    if request.method == 'POST':
        candidate = get_object_or_404(Candidate, id=candidate_id)
        try:
            amount = float(request.POST.get('amount', 0))
        except (TypeError, ValueError):
            messages.error(request, "Please enter a valid amount.")
            return redirect('event_detail', event_id=candidate.event.id)

        if amount < 1:
            messages.error(request, "Please enter a valid amount.")
            return redirect('event_detail', event_id=candidate.event.id)
            
        event = candidate.event
        vote_price = float(event.vote_price)
        if vote_price <= 0: vote_price = 1.00
        votes_requested = int(amount / vote_price)
        
        if votes_requested < 1:
            messages.error(request, f"Minimum amount is ₵{vote_price} for 1 vote.")
            return redirect('event_detail', event_id=event.id)
            
        voter_email = "anonymous@FlexyVotes.com"
        auth_url, reference = initialize_paystack_payment(voter_email, amount, candidate_id)
        
        if auth_url:
            VoteTransaction.objects.create(
                candidate=candidate, voter_email=voter_email, amount=amount,
                paystack_reference=reference, status='Pending', number_of_votes=votes_requested
            )
            return redirect(auth_url)
        else:
            return redirect('event_detail', event_id=candidate.event.id)
    return redirect('home')

def vote_success(request):
    reference = request.GET.get('reference')
    if reference:
        transaction = VoteTransaction.objects.filter(paystack_reference=reference).first()
        if transaction:
            if transaction.status == 'Pending':
                if verify_paystack_transaction(reference):
                    transaction.status = 'Success'
                    transaction.save()
                else:
                    messages.error(request, 'We could not confirm your payment yet. If you were charged, your vote will be credited shortly once confirmed.')
                    return redirect('event_detail', event_id=transaction.candidate.event.id)
            event_id = transaction.candidate.event.id
            messages.success(request, 'Payment successful! Your vote has been cast.')
            return redirect('event_detail', event_id=event_id)
    messages.success(request, 'Payment successful! Your vote has been cast.')
    return redirect('home')

@csrf_exempt
def paystack_webhook(request):
    if request.method == 'POST':
        signature = request.headers.get('x-paystack-signature', '')
        secret = settings.PAYSTACK_SECRET_KEY
        computed_signature = hmac.new(secret.encode('utf-8'), request.body, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(computed_signature, signature):
            return HttpResponse(status=400)
        try:
            payload = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return HttpResponse(status=400)
        if payload.get('event') == 'charge.success':
            data = payload.get('data', {})
            reference = data.get('reference')
            if data.get('metadata', {}).get('type') == 'ticket_purchase':
                try:
                    purchase = TicketPurchase.objects.get(paystack_reference=reference)
                    if purchase.status == 'Pending':
                        purchase.status = 'Success'
                        purchase.save()
                except TicketPurchase.DoesNotExist:
                    pass
            else:
                try:
                    transaction = VoteTransaction.objects.get(paystack_reference=reference)
                    if transaction.status == 'Pending':
                        transaction.status = 'Success'
                        transaction.save()
                except VoteTransaction.DoesNotExist:
                    pass
        return HttpResponse(status=200)
    return HttpResponse(status=400)

def live_vote_counts(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    candidates = event.candidates.annotate(
        vote_count=Coalesce(Sum('transactions__number_of_votes', filter=Q(transactions__status='Success', transactions__vote_type='Main')), 0, output_field=IntegerField()),
        tie_breaker_count=Coalesce(Sum('transactions__number_of_votes', filter=Q(transactions__status='Success', transactions__vote_type='Tie-Breaker')), 0, output_field=IntegerField())
    ).order_by('-vote_count', '-tie_breaker_count', 'name')
    
    data = [{'id': c.id, 'name': c.name, 'votes': c.vote_count, 'tie_breakers': c.tie_breaker_count} for c in candidates]
    total_votes = sum(c['votes'] for c in data)
    for c in data:
        c['percentage'] = int((c['votes'] / total_votes) * 100) if total_votes > 0 else 0
    return JsonResponse({'candidates': data, 'total_votes': total_votes})

def login_view(request):
    ip_address = request.META.get('REMOTE_ADDR')
    cache_key = f'login_attempts_{ip_address}'
    attempts = cache.get(cache_key, 0)
    if attempts >= 5:
        messages.error(request, "Too many login attempts. Please wait a minute and try again.")
        return render(request, 'voting/login.html')
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            cache.delete(cache_key)
            login(request, user)
            return redirect('home')
        else:
            cache.set(cache_key, attempts + 1, 60)
            messages.error(request, 'Invalid username or password.')
    return render(request, 'voting/login.html')

def register_view(request):
    ip_address = request.META.get('REMOTE_ADDR')
    cache_key = f'register_attempts_{ip_address}'
    attempts = cache.get(cache_key, 0)
    if attempts >= 3:
        messages.error(request, "Too many registration attempts. Please wait a minute and try again.")
        return render(request, 'voting/register.html')
    if request.method == 'POST':
        cache.set(cache_key, attempts + 1, 60)
        username = request.POST.get('username')
        email = request.POST.get('email')
        password = request.POST.get('password')
        if User.objects.filter(username=username).exists():
            messages.error(request, 'Username already taken.')
        elif not password:
            messages.error(request, 'Password is required.')
        else:
            try:
                validate_password(password)
            except ValidationError as e:
                for error in e.messages:
                    messages.error(request, error)
                return render(request, 'voting/register.html')
            user = User.objects.create_user(username=username, email=email, password=password)
            Profile.objects.create(user=user)
            login(request, user)
            admin_emails = User.objects.filter(is_superuser=True).exclude(email='').values_list('email', flat=True)
            if admin_emails:
                subject = "New Organizer Registration Pending Approval"
                from_email = settings.DEFAULT_FROM_EMAIL
                text_content = f"A new user has registered on FlexyVotes and requires approval.\n\nUsername: {username}\nEmail: {email}\n\nPlease log in to the admin panel to review and approve them."
                html_content = f"""
                <div style="width: 100%; background-color: #F5F5F5; padding: 20px; font-family: Arial, sans-serif;">
                    <div style="max-width: 600px; margin: 0 auto; background-color: #FFFFFF; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 10px rgba(0,0,0,0.1);">
                        <div style="background-color: #800020; padding: 20px; text-align: center;">
                            <h1 style="color: #FFD700; margin: 0; font-size: 28px; letter-spacing: 1px;">FlexyVotes</h1>
                        </div>
                        <div style="padding: 30px; color: #1E1E1E; line-height: 1.6;">
                            <h2 style="color: #800020; margin-top: 0;">New Organizer Registration</h2>
                            <p style="font-size: 16px;">A new user has registered on the platform and is requesting organizer access.</p>
                            <div style="background-color: #f8f9fa; border-left: 4px solid #800020; padding: 15px; margin: 20px 0; border-radius: 4px;">
                                <p style="margin: 0; font-size: 16px;"><b>Username:</b> {username}</p>
                                <p style="margin: 5px 0 0 0; font-size: 16px;"><b>Email:</b> {email}</p>
                            </div>
                            <p style="font-size: 16px;">Please log in to the admin dashboard to review and approve this user.</p>
                            <a href="{settings.SITE_URL}/admin/auth/user/" style="display: inline-block; background-color: #800020; color: #FFD700; padding: 12px 30px; text-decoration: none; border-radius: 30px; font-weight: bold; margin-top: 10px;">Review User</a>
                        </div>
                        <div style="background-color: #1E1E1E; color: #888; padding: 20px; text-align: center; font-size: 12px;">&copy; FlexyVotes. All rights reserved.</div>
                    </div>
                </div>
                """
                msg = EmailMultiAlternatives(subject, text_content, from_email, list(admin_emails))
                msg.attach_alternative(html_content, "text/html")
                try:
                    msg.send(fail_silently=True)
                except Exception:
                    pass
            messages.success(request, 'Registration successful! Your organizer account is pending admin approval.')
            return redirect('home')
    return render(request, 'voting/register.html')

def logout_view(request):
    logout(request)
    return redirect('home')

@login_required(login_url='/login/')
def dashboard(request):
    if request.user.is_staff:
        user_events = Event.objects.all().order_by('-start_date')
        recent_logs = ActivityLog.objects.all().order_by('-created_at')[:5]
    else:
        is_approved = hasattr(request.user, 'profile') and request.user.profile.is_approved_organizer
        if not is_approved:
            messages.error(request, 'Your organizer account is pending admin approval.')
            return redirect('home')
        user_events = Event.objects.filter(organizer=request.user).order_by('-start_date')
        recent_logs = ActivityLog.objects.filter(user=request.user).order_by('-created_at')[:5]
    return render(request, 'voting/dashboard.html', {'events': user_events, 'logs': recent_logs})

@login_required(login_url='/login/')
def approve_event(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if not request.user.is_staff:
        return redirect('dashboard')
    if request.method == 'POST' and not event.is_approved:
        event.is_approved = True
        event.save()
        ActivityLog.objects.create(user=request.user, event=event, action=f"Approved event '{event.title}'")
        messages.success(request, f'"{event.title}" has been approved and is now visible to voters.')
    return redirect('dashboard')

@login_required(login_url='/login/')
def create_event(request):
    is_approved = hasattr(request.user, 'profile') and request.user.profile.is_approved_organizer
    if not is_approved and not request.user.is_staff:
        return redirect('home')
    if request.method == 'POST':
        start_date_str = f"{request.POST.get('start_date_date')} {request.POST.get('start_date_time')}"
        end_date_str = f"{request.POST.get('end_date_date')} {request.POST.get('end_date_time')}"
        voting_mode = request.POST.get('voting_mode')
        
        # If Code Voting is selected, fee and price are not needed
        if voting_mode == 'Code Voting':
            platform_fee_percentage = Decimal('0')
            vote_price = Decimal('0')
            enable_tie_breaker = False
        else:
            platform_fee_percentage, fee_error = parse_non_negative_decimal(request.POST.get('platform_fee_percentage'), 'Platform fee percentage')
            vote_price, price_error = parse_non_negative_decimal(request.POST.get('vote_price', '1.00'), 'Vote price')
            enable_tie_breaker = request.POST.get('enable_tie_breaker') == 'on'
            error = fee_error or price_error
            if error:
                messages.error(request, error)
                return render(request, 'voting/create_event.html')

        event = Event.objects.create(
            organizer=request.user,
            title=request.POST.get('title'),
            description=request.POST.get('description'),
            voting_mode=voting_mode,
            code_voting_mode=request.POST.get('code_voting_mode', 'Standard'),
            enable_tie_breaker=enable_tie_breaker,
            start_date=timezone.make_aware(parse_datetime(start_date_str)),
            end_date=timezone.make_aware(parse_datetime(end_date_str)),
            platform_fee_percentage=platform_fee_percentage,
            vote_price=vote_price,
            primary_color=request.POST.get('primary_color'),
            accent_color=request.POST.get('accent_color'),
            background_image=request.FILES.get('background_image'),
            event_image=request.FILES.get('event_image')
        )
        ActivityLog.objects.create(user=request.user, event=event, action=f"Created event '{event.title}'")
        return redirect('dashboard')
    return render(request, 'voting/create_event.html')

@login_required(login_url='/login/')
def add_candidate(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event_id)
    if request.method == 'POST':
        name = request.POST.get('name')
        bio = request.POST.get('bio')
        image = request.FILES.get('image')
        category_id = request.POST.get('category')
        nominee_code = request.POST.get('nominee_code')
        category = Category.objects.get(id=category_id) if category_id else None
        candidate = Candidate.objects.create(event=event, name=name, bio=bio, image=image, category=category, nominee_code=nominee_code)
        ActivityLog.objects.create(user=request.user, event=event, action=f"Added candidate '{candidate.name}' to '{event.title}'")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'status': 'success'})
    return redirect('event_detail', event_id=event_id)

@login_required(login_url='/login/')
def edit_candidate(request, candidate_id):
    candidate = get_object_or_404(Candidate, id=candidate_id)
    event = candidate.event
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event.id)
    if request.method == 'POST':
        candidate.name = request.POST.get('name')
        candidate.bio = request.POST.get('bio')
        candidate.nominee_code = request.POST.get('nominee_code')
        category_id = request.POST.get('category')
        candidate.category = Category.objects.get(id=category_id) if category_id else None
        if 'image' in request.FILES:
            candidate.image = request.FILES.get('image')
        candidate.save()
        ActivityLog.objects.create(user=request.user, event=event, action=f"Updated candidate '{candidate.name}' in '{event.title}'")
        return redirect('event_detail', event_id=event.id)
    return render(request, 'voting/edit_candidate.html', {'candidate': candidate, 'event': event})

@login_required(login_url='/login/')
def event_analytics(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event_id)
    categories = event.categories.all()
    chart_data = []
    for category in categories:
        candidates = category.candidates.annotate(
            main_votes=Coalesce(Sum('transactions__number_of_votes', filter=Q(transactions__status='Success', transactions__vote_type='Main')), 0, output_field=IntegerField()),
            tie_breaker_votes=Coalesce(Sum('transactions__number_of_votes', filter=Q(transactions__status='Success', transactions__vote_type='Tie-Breaker')), 0, output_field=IntegerField())
        ).order_by('-main_votes', '-tie_breaker_votes')
        chart_data.append({
            'category_name': category.name, 'labels': [c.name for c in candidates],
            'main_data': [c.main_votes for c in candidates], 'tie_breaker_data': [c.tie_breaker_votes for c in candidates]
        })
    all_candidates = event.candidates.annotate(
        main_votes=Coalesce(Sum('transactions__number_of_votes', filter=Q(transactions__status='Success', transactions__vote_type='Main')), 0, output_field=IntegerField()),
        tie_breaker_votes=Coalesce(Sum('transactions__number_of_votes', filter=Q(transactions__status='Success', transactions__vote_type='Tie-Breaker')), 0, output_field=IntegerField()),
        revenue=Coalesce(Sum('transactions__amount', filter=Q(transactions__status='Success')), 0, output_field=IntegerField())
    ).order_by('category__name', '-main_votes')
    return render(request, 'voting/analytics.html', {'event': event, 'chart_data': chart_data, 'all_candidates': all_candidates})

@login_required(login_url='/login/')
def add_category(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event_id)
    if request.method == 'POST':
        name = request.POST.get('name')
        if name:
            category = Category.objects.create(event=event, name=name)
            ActivityLog.objects.create(user=request.user, event=event, action=f"Created category '{category.name}' under '{event.title}'")
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'status': 'success'})
    return redirect('event_detail', event_id=event_id)

@login_required(login_url='/login/')
def edit_event(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event_id)
    if request.method == 'POST':
        start_date_str = f"{request.POST.get('start_date_date')} {request.POST.get('start_date_time')}"
        end_date_str = f"{request.POST.get('end_date_date')} {request.POST.get('end_date_time')}"
        voting_mode = request.POST.get('voting_mode')
        
        if voting_mode == 'Code Voting':
            platform_fee_percentage = Decimal('0')
            vote_price = Decimal('0')
            enable_tie_breaker = False
        else:
            platform_fee_percentage, fee_error = parse_non_negative_decimal(request.POST.get('platform_fee_percentage'), 'Platform fee percentage')
            vote_price, price_error = parse_non_negative_decimal(request.POST.get('vote_price', '1.00'), 'Vote price')
            enable_tie_breaker = request.POST.get('enable_tie_breaker') == 'on'
            error = fee_error or price_error
            if error:
                messages.error(request, error)
                return render(request, 'voting/edit_event.html', {'event': event})

        event.title = request.POST.get('title')
        event.description = request.POST.get('description')
        event.voting_mode = voting_mode
        event.code_voting_mode = request.POST.get('code_voting_mode', 'Standard')
        event.enable_tie_breaker = enable_tie_breaker
        event.start_date = timezone.make_aware(parse_datetime(start_date_str))
        event.end_date = timezone.make_aware(parse_datetime(end_date_str))
        event.platform_fee_percentage = platform_fee_percentage
        event.vote_price = vote_price
        event.primary_color = request.POST.get('primary_color')
        event.accent_color = request.POST.get('accent_color')
        
        if 'background_image' in request.FILES:
            event.background_image = request.FILES.get('background_image')
        if 'event_image' in request.FILES:
            event.event_image = request.FILES.get('event_image')
        event.save()
        ActivityLog.objects.create(user=request.user, event=event, action=f"Updated event details for '{event.title}'")
        return redirect('dashboard')
    return render(request, 'voting/edit_event.html', {'event': event})

@login_required(login_url='/login/')
def edit_category(request, category_id):
    category = get_object_or_404(Category, id=category_id)
    event = category.event
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event.id)
    if request.method == 'POST':
        category.name = request.POST.get('name')
        category.save()
        ActivityLog.objects.create(user=request.user, event=event, action=f"Updated category '{category.name}' in '{event.title}'")
        return redirect('event_detail', event_id=event.id)
    return render(request, 'voting/edit_category.html', {'category': category, 'event': event})

def contact_view(request):
    if request.method == 'POST':
        messages.success(request, 'Your message has been sent! We will get back to you shortly.')
        return redirect('contact')
    return render(request, 'voting/contact.html')

def store_view(request):
    categories = ProductCategory.objects.all()
    selected_category = request.GET.get('category')
    if selected_category:
        products = Product.objects.filter(is_active=True, category__name=selected_category).order_by('-created_at')
    else:
        products = Product.objects.filter(is_active=True).order_by('-created_at')
    return render(request, 'voting/store.html', {'products': products, 'categories': categories, 'selected_category': selected_category})

@login_required(login_url='/login/')
def manage_store(request):
    if not request.user.is_staff:
        return redirect('home')
    products = Product.objects.all().order_by('-created_at')
    categories = ProductCategory.objects.all()
    return render(request, 'voting/manage_store.html', {'products': products, 'categories': categories})

@login_required(login_url='/login/')
def add_product(request):
    if not request.user.is_staff:
        return redirect('home')
    if request.method == 'POST':
        price, price_error = parse_non_negative_decimal(request.POST.get('price'), 'Price')
        old_price, old_price_error = parse_non_negative_decimal(request.POST.get('old_price'), 'Old price', required=False)
        error = price_error or old_price_error
        if error:
            messages.error(request, error)
            return render(request, 'voting/add_product.html', {'categories': ProductCategory.objects.all()})
        category_id = request.POST.get('category')
        category = ProductCategory.objects.get(id=category_id) if category_id else None
        product = Product.objects.create(
            name=request.POST.get('name'), description=request.POST.get('description'),
            price=price, old_price=old_price, image=request.FILES.get('image'),
            category=category, is_active=request.POST.get('is_active') == 'on'
        )
        images = request.FILES.getlist('additional_images')
        for img in images:
            ProductImage.objects.create(product=product, image=img)
        return redirect('manage_store')
    categories = ProductCategory.objects.all()
    return render(request, 'voting/add_product.html', {'categories': categories})

@login_required(login_url='/login/')
def edit_product(request, product_id):
    if not request.user.is_staff:
        return redirect('home')
    product = get_object_or_404(Product, id=product_id)
    if request.method == 'POST':
        price, price_error = parse_non_negative_decimal(request.POST.get('price'), 'Price')
        old_price, old_price_error = parse_non_negative_decimal(request.POST.get('old_price'), 'Old price', required=False)
        error = price_error or old_price_error
        if error:
            messages.error(request, error)
            return render(request, 'voting/edit_product.html', {'product': product, 'categories': ProductCategory.objects.all()})
        product.name = request.POST.get('name')
        product.description = request.POST.get('description')
        product.price = price
        product.old_price = old_price
        category_id = request.POST.get('category')
        product.category = ProductCategory.objects.get(id=category_id) if category_id else None
        if 'image' in request.FILES:
            product.image = request.FILES.get('image')
        product.is_active = request.POST.get('is_active') == 'on'
        product.save()
        images = request.FILES.getlist('additional_images')
        for img in images:
            ProductImage.objects.create(product=product, image=img)
        return redirect('manage_store')
    categories = ProductCategory.objects.all()
    return render(request, 'voting/edit_product.html', {'product': product, 'categories': categories})

@login_required(login_url='/login/')
def add_product_category(request):
    if not request.user.is_staff:
        return redirect('home')
    if request.method == 'POST':
        name = request.POST.get('name')
        if name:
            ProductCategory.objects.create(name=name)
            messages.success(request, f'Category "{name}" added successfully.')
    return redirect('manage_store')

@login_required(login_url='/login/')
def generate_codes(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event_id)
    if request.method == 'POST':
        identifiers = request.POST.get('identifiers', '').strip()
        if identifiers:
            id_list = [line.strip() for line in identifiers.split('\n') if line.strip()]
            generated_count = 0
            for identifier in id_list:
                code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
                while VotingCode.objects.filter(code=code).exists():
                    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
                VotingCode.objects.create(event=event, code=code, voter_identifier=identifier)
                generated_count += 1
            messages.success(request, f'{generated_count} codes generated successfully for the provided IDs.')
        else:
            count, count_error = parse_non_negative_int(request.POST.get('count', '10'), 'Count')
            if count_error:
                messages.error(request, count_error)
                return redirect('event_detail', event_id=event_id)
            count = min(count, 500)
            generated_count = 0
            while generated_count < count:
                code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
                if not VotingCode.objects.filter(code=code).exists():
                    VotingCode.objects.create(event=event, code=code)
                    generated_count += 1
            messages.success(request, f'{count} standard voting codes generated successfully.')
    return redirect('event_detail', event_id=event_id)

@login_required(login_url='/login/')
def download_codes(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event_id)
    codes = VotingCode.objects.filter(event=event).order_by('created_at')
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{event.title}_codes.csv"'
    writer = csv.writer(response)
    writer.writerow(['Code', 'Student ID / Identifier', 'Status', 'Used At'])
    for code in codes:
        writer.writerow([sanitize_csv_value(code.code), sanitize_csv_value(code.voter_identifier or ''), 'Used' if code.is_used else 'Valid', code.used_at])
    return response

def cast_vote_with_code(request, candidate_id):
    candidate = get_object_or_404(Candidate, id=candidate_id)
    if request.method == 'POST':
        event = candidate.event
        code_input = request.POST.get('code', '').strip().upper()
        identifier_input = request.POST.get('identifier', '').strip()
        if timezone.now() > event.end_date:
            messages.error(request, "Voting for this event has ended.")
            return redirect('event_detail', event_id=event.id)
        
        # FLOW 1: TICKET REFERENCE (TIE-BREAKER VOTE)
        if code_input.startswith('TK-'):
            ticket_purchase = TicketPurchase.objects.filter(paystack_reference=code_input, event=event).first()
            if not ticket_purchase:
                messages.error(request, "Invalid Ticket Reference. Please check your ticket.")
                return redirect('event_detail', event_id=event.id)
            if ticket_purchase.status != 'Success':
                messages.error(request, "This ticket payment is still pending or failed.")
                return redirect('event_detail', event_id=event.id)
            if ticket_purchase.has_voted:
                messages.error(request, "This ticket has already been used to vote!")
                return redirect('event_detail', event_id=event.id)
            if not event.enable_tie_breaker:
                messages.error(request, "Ticket votes are not enabled for this event.")
                return redirect('event_detail', event_id=event.id)
            if ticket_purchase.purchase_method != 'Web':
                messages.error(request, "Only online tickets are eligible for the free vote.")
                return redirect('event_detail', event_id=event.id)
            votes_to_cast = ticket_purchase.quantity
            VoteTransaction.objects.create(
                candidate=candidate, voter_email=f"ticket_{code_input}@FlexyVotes.com", amount=0,
                paystack_reference=f"TIE_{code_input}_{uuid.uuid4().hex[:4].upper()}", status='Success',
                vote_type='Tie-Breaker', number_of_votes=votes_to_cast
            )
            ticket_purchase.has_voted = True
            ticket_purchase.save()
            messages.success(request, f"Success! Your {votes_to_cast} free vote(s) for {candidate.name} has been cast.")
            return redirect('event_detail', event_id=event.id)
            
        # FLOW 2: STANDARD VOTING CODE (Legacy fallback if modal isn't used)
        try:
            voting_code = VotingCode.objects.get(event=event, code=code_input)
            if voting_code.voter_identifier:
                if voting_code.voter_identifier.upper() != identifier_input.upper():
                    messages.error(request, "The Student ID provided does not match this voting code.")
                    return redirect('event_detail', event_id=event.id)
            if voting_code.is_used:
                messages.error(request, "This code has already been used.")
            else:
                voting_code.is_used = True
                voting_code.used_at = timezone.now()
                voting_code.save()
                VoteTransaction.objects.create(
                    candidate=candidate, voter_email=f"code_{code_input}@FlexyVotes.com", amount=0,
                    paystack_reference=f"TIE_{code_input}_{uuid.uuid4().hex[:4].upper()}", status='Success',
                    vote_type='Main', number_of_votes=1
                )
                messages.success(request, f"Success! Your vote for {candidate.name} has been cast.")
        except VotingCode.DoesNotExist:
            messages.error(request, "Invalid voting code. Please check and try again.")
    return redirect('event_detail', event_id=candidate.event.id)

@login_required(login_url='/login/')
def clear_codes(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event_id)
    if request.method == 'POST':
        event.voting_codes.all().delete()
        messages.success(request, 'All voting codes have been cleared.')
    return redirect('event_detail', event_id=event_id)

@login_required(login_url='/login/')
def upload_student_csv(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event_id)
    if request.method == 'POST':
        csv_file = request.FILES.get('csv_file')
        if not csv_file.name.endswith('.csv'):
            messages.error(request, 'Please upload a valid .csv file.')
            return redirect('event_detail', event_id=event_id)
        decoded_file = csv_file.read().decode('utf-8').splitlines()
        reader = csv.reader(decoded_file)
        generated_count = 0
        for row in reader:
            if row and row[0].strip():
                identifier = row[0].strip()
                code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
                while VotingCode.objects.filter(code=code).exists():
                    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
                VotingCode.objects.create(event=event, code=code, voter_identifier=identifier)
                generated_count += 1
        messages.success(request, f'{generated_count} codes generated successfully from the CSV file.')
    return redirect('event_detail', event_id=event_id)

@csrf_exempt
def ussd_callback(request):
    session_id = request.POST.get("sessionId", "")
    service_code = request.POST.get("serviceCode", "")
    phone_number = request.POST.get("phoneNumber", "")
    text = request.POST.get("text", "")
    inputs = text.split('*') if text else []
    
    if text == "":
        menu = "CON Welcome to FlexyVotes.\n1. Vote for Candidate\n2. Buy Event Ticket"
        return HttpResponse(menu, content_type='text/plain')
        
    first_input = inputs[0]
    
    if first_input == "1":
        if len(inputs) == 1:
            menu = "CON Enter Nominee Code:"
            return HttpResponse(menu, content_type='text/plain')
        elif len(inputs) == 2:
            code_input = inputs[1].strip()
            candidate = Candidate.objects.filter(nominee_code=code_input).first()
            if candidate:
                menu = f"CON You selected {candidate.name}.\nEnter number of votes (1 GHS = 1 vote):"
                return HttpResponse(menu, content_type='text/plain')
            else:
                return HttpResponse("END Invalid Nominee Code. Please check and try again.", content_type='text/plain')
        elif len(inputs) == 3:
            try:
                votes = int(inputs[2])
                candidate = Candidate.objects.filter(nominee_code=inputs[1]).first()
                if candidate and votes > 0:
                    total_cost = votes * 1
                    menu = f"CON Pay GHS {total_cost} for {votes} votes for {candidate.name}?\n1. Confirm\n2. Cancel"
                    return HttpResponse(menu, content_type='text/plain')
                else:
                    return HttpResponse("END Invalid number of votes.", content_type='text/plain')
            except ValueError:
                return HttpResponse("END Invalid input. Please enter a number.", content_type='text/plain')
        elif len(inputs) == 4:
            if inputs[3] == "1":
                try:
                    code_input = inputs[1]
                    votes = int(inputs[2])
                    candidate = Candidate.objects.filter(nominee_code=code_input).first()
                    if candidate and votes > 0:
                        amount = votes * 1
                        reference = f"USSD_{uuid.uuid4().hex[:8].upper()}"
                        VoteTransaction.objects.create(
                            candidate=candidate, voter_email=f"{phone_number}@ussd.vote", amount=amount,
                            paystack_reference=reference, status='Success', number_of_votes=votes
                        )
                        return HttpResponse(f"END Payment successful! You have cast {votes} votes for {candidate.name}.", content_type='text/plain')
                    else:
                        return HttpResponse("END Invalid selection.", content_type='text/plain')
                except (ValueError, IndexError):
                    return HttpResponse("END An error occurred. Please try again.", content_type='text/plain')
            else:
                return HttpResponse("END Transaction cancelled.", content_type='text/plain')

    elif first_input == "2":
        if len(inputs) == 1:
            events = Event.objects.filter(is_active=True, is_approved=True, tickets__isnull=False).distinct().order_by('id')
            if not events:
                return HttpResponse("END No events are selling tickets right now.", content_type='text/plain')
            menu = "CON Select Event:\n"
            for i, event in enumerate(events):
                menu += f"{i+1}. {event.title}\n"
            return HttpResponse(menu, content_type='text/plain')
        elif len(inputs) == 2:
            try:
                event_index = int(inputs[1]) - 1
                events = Event.objects.filter(is_active=True, is_approved=True, tickets__isnull=False).distinct().order_by('id')
                if 0 <= event_index < len(events):
                    selected_event = events[event_index]
                    tickets = selected_event.tickets.filter(is_active=True).order_by('id')
                    if not tickets:
                        return HttpResponse("END No tickets available for this event.", content_type='text/plain')
                    menu = f"CON Select Ticket for {selected_event.title}:\n"
                    for i, ticket in enumerate(tickets):
                        menu += f"{i+1}. {ticket.name} - GHS {ticket.price}\n"
                    return HttpResponse(menu, content_type='text/plain')
                else:
                    return HttpResponse("END Invalid event selected.", content_type='text/plain')
            except ValueError:
                return HttpResponse("END Invalid input.", content_type='text/plain')
        elif len(inputs) == 3:
            try:
                event_index = int(inputs[1]) - 1
                events = Event.objects.filter(is_active=True, is_approved=True, tickets__isnull=False).distinct().order_by('id')
                selected_event = events[event_index]
                ticket_index = int(inputs[2]) - 1
                tickets = selected_event.tickets.filter(is_active=True).order_by('id')
                if 0 <= ticket_index < len(tickets):
                    selected_ticket = tickets[ticket_index]
                    menu = f"CON You selected {selected_ticket.name} (GHS {selected_ticket.price}).\nEnter Quantity:"
                    return HttpResponse(menu, content_type='text/plain')
                else:
                    return HttpResponse("END Invalid ticket selected.", content_type='text/plain')
            except (ValueError, IndexError):
                return HttpResponse("END An error occurred.", content_type='text/plain')
        elif len(inputs) == 4:
            try:
                event_index = int(inputs[1]) - 1
                events = Event.objects.filter(is_active=True, is_approved=True, tickets__isnull=False).distinct().order_by('id')
                selected_event = events[event_index]
                ticket_index = int(inputs[2]) - 1
                tickets = selected_event.tickets.filter(is_active=True).order_by('id')
                selected_ticket = tickets[ticket_index]
                quantity = int(inputs[3])
                if quantity < 1:
                    return HttpResponse("END Invalid quantity.", content_type='text/plain')
                total_cost = selected_ticket.price * quantity
                menu = f"CON Total: GHS {total_cost} for {quantity} {selected_ticket.name}.\nEnter your full name:"
                return HttpResponse(menu, content_type='text/plain')
            except (ValueError, IndexError):
                return HttpResponse("END An error occurred.", content_type='text/plain')
        elif len(inputs) == 5:
            try:
                event_index = int(inputs[1]) - 1
                events = Event.objects.filter(is_active=True, is_approved=True, tickets__isnull=False).distinct().order_by('id')
                selected_event = events[event_index]
                ticket_index = int(inputs[2]) - 1
                tickets = selected_event.tickets.filter(is_active=True).order_by('id')
                selected_ticket = tickets[ticket_index]
                quantity = int(inputs[3])
                buyer_name = inputs[4]
                total_cost = selected_ticket.price * quantity
                menu = f"CON Pay GHS {total_cost} for {quantity} {selected_ticket.name} for {buyer_name}?\n1. Confirm\n2. Cancel"
                return HttpResponse(menu, content_type='text/plain')
            except (ValueError, IndexError):
                return HttpResponse("END An error occurred.", content_type='text/plain')
        elif len(inputs) == 6:
            if inputs[5] == "1":
                try:
                    event_index = int(inputs[1]) - 1
                    events = Event.objects.filter(is_active=True, is_approved=True, tickets__isnull=False).distinct().order_by('id')
                    selected_event = events[event_index]
                    ticket_index = int(inputs[2]) - 1
                    tickets = selected_event.tickets.filter(is_active=True).order_by('id')
                    selected_ticket = tickets[ticket_index]
                    quantity = int(inputs[3])
                    buyer_name = inputs[4]
                    letters = ''.join(random.choices(string.ascii_uppercase, k=2))
                    numbers = ''.join(random.choices(string.digits, k=4))
                    reference = f"TK-{letters}{numbers}"
                    already_sold = TicketPurchase.objects.filter(ticket=selected_ticket, status='Success').aggregate(total=Sum('quantity'))['total'] or 0
                    if already_sold + quantity > selected_ticket.quantity_available:
                        return HttpResponse("END Sorry, not enough tickets available for this request.", content_type='text/plain')
                    TicketPurchase.objects.create(
                        ticket=selected_ticket, event=selected_event, buyer_name=buyer_name, buyer_email=f"{phone_number}@ussd.vote",
                        quantity=quantity, paystack_reference=reference, status='Success', purchase_method='USSD'
                    )
                    return HttpResponse(f"END Success! {quantity} {selected_ticket.name} tickets bought for {buyer_name}.\nRef: {reference}\nVisit FlexyVotes.com/retrieve to view ticket.", content_type='text/plain')
                except Exception:
                    return HttpResponse("END An error occurred during processing.", content_type='text/plain')
            else:
                return HttpResponse("END Transaction cancelled.", content_type='text/plain')
    return HttpResponse("END Invalid request.", content_type='text/plain')

@login_required(login_url='/login/')
def create_ticket(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event_id)
    if request.method == 'POST':
        name = request.POST.get('name')
        price, price_error = parse_non_negative_decimal(request.POST.get('price'), 'Price')
        old_price, old_price_error = parse_non_negative_decimal(request.POST.get('old_price'), 'Old price', required=False)
        quantity, quantity_error = parse_non_negative_int(request.POST.get('quantity_available'), 'Quantity available')
        error = price_error or old_price_error or quantity_error
        if error:
            messages.error(request, error)
            return render(request, 'voting/create_ticket.html', {'event': event})
        image = request.FILES.get('image')
        Ticket.objects.create(
            event=event, name=name, price=price, old_price=old_price, quantity_available=quantity, image=image
        )
        messages.success(request, f'Ticket type "{name}" added successfully.')
        return redirect('event_detail', event_id=event_id)
    return render(request, 'voting/create_ticket.html', {'event': event})

def buy_ticket(request, ticket_id):
    if request.method == 'POST':
        ticket = get_object_or_404(Ticket, id=ticket_id)
        buyer_name = request.POST.get('name')
        buyer_email = request.POST.get('email')
        try:
            quantity = int(request.POST.get('quantity', 1))
        except (TypeError, ValueError):
            quantity = 0
        if quantity < 1:
            messages.error(request, "Please enter a valid quantity.")
            return redirect('event_tickets', event_id=ticket.event.id)
        already_sold = TicketPurchase.objects.filter(ticket=ticket, status='Success').aggregate(total=Sum('quantity'))['total'] or 0
        if already_sold + quantity > ticket.quantity_available:
            messages.error(request, "Not enough tickets available for this request.")
            return redirect('event_tickets', event_id=ticket.event.id)
        total_amount = ticket.price * quantity
        auth_url, reference = initialize_ticket_payment(buyer_email, total_amount, ticket_id, quantity)
        if auth_url:
            TicketPurchase.objects.create(
                ticket=ticket, event=ticket.event, buyer_name=buyer_name, buyer_email=buyer_email,
                quantity=quantity, paystack_reference=reference, status='Pending'
            )
            return redirect(auth_url)
        messages.error(request, "Could not initialize payment. Please try again.")
        return redirect('event_tickets', event_id=ticket.event.id)
    return redirect('tickets')

def tickets_view(request):
    events_with_tickets = Event.objects.filter(is_active=True, is_approved=True, tickets__isnull=False).distinct().order_by('-start_date')
    ticket_found = None
    error_message = None
    active_action = None
    if request.method == 'POST':
        action = request.POST.get('action')
        active_action = action
        if action == 'verify':
            reference = request.POST.get('reference', '').strip().upper()
            purchase = TicketPurchase.objects.filter(paystack_reference=reference).first()
            if purchase:
                if purchase.status == 'Success':
                    ticket_found = purchase
                else:
                    error_message = "This ticket payment is still pending or failed."
            else:
                error_message = "Invalid reference code. No ticket found."
        elif action == 'retrieve':
            phone_or_ref = request.POST.get('phone_or_ref', '').strip()
            if phone_or_ref.upper().startswith('TK-'):
                purchase = TicketPurchase.objects.filter(paystack_reference=phone_or_ref.upper()).first()
            else:
                clean_phone = phone_or_ref.replace('+', '').replace(' ', '')
                purchase = TicketPurchase.objects.filter(buyer_email=f"{clean_phone}@ussd.vote").first()
            if purchase:
                if purchase.status == 'Success':
                    return redirect(f"/ticket/success/?reference={purchase.paystack_reference}")
                else:
                    error_message = "This ticket payment is still pending or failed."
            else:
                error_message = "No ticket found. Please check your Reference Code or Phone Number."
    return render(request, 'voting/tickets.html', {'events': events_with_tickets, 'ticket_found': ticket_found, 'error_message': error_message, 'active_action': active_action})

def event_tickets_view(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    tickets = event.tickets.filter(is_active=True)
    return render(request, 'voting/event_tickets.html', {'event': event, 'tickets': tickets})

@login_required(login_url='/login/')
def event_guestlist(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event_id)
    purchases = TicketPurchase.objects.filter(event=event, status='Success').order_by('-purchased_at')
    return render(request, 'voting/guestlist.html', {'event': event, 'purchases': purchases})

@login_required(login_url='/login/')
def download_guestlist(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event_id)
    purchases = TicketPurchase.objects.filter(event=event, status='Success').order_by('-purchased_at')
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{event.title}_guestlist.csv"'
    writer = csv.writer(response)
    writer.writerow(['Buyer Name', 'Buyer Email', 'Ticket Type', 'Quantity', 'Reference', 'Purchased At'])
    for p in purchases:
        writer.writerow([sanitize_csv_value(p.buyer_name), sanitize_csv_value(p.buyer_email), sanitize_csv_value(p.ticket.name), p.quantity, sanitize_csv_value(p.paystack_reference), p.purchased_at])
    return response

def ticket_success(request):
    reference = request.GET.get('reference')
    if not reference:
        return redirect('home')
    purchase = TicketPurchase.objects.filter(paystack_reference=reference).first()
    if not purchase:
        return redirect('home')
    just_paid = False
    if purchase.status == 'Pending':
        if verify_paystack_transaction(reference):
            purchase.status = 'Success'
            purchase.save()
            just_paid = True
        else:
            messages.error(request, 'We could not confirm your payment yet. If you were charged, your ticket will be confirmed shortly.')
            return redirect('tickets')
    qr_data = f"EVENT: {purchase.ticket.event.title}\nNAME: {purchase.buyer_name}\nTICKET: {purchase.ticket.name}\nQTY: {purchase.quantity}\nREF: {purchase.paystack_reference}"
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    qr_code_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
    return render(request, 'voting/ticket_success.html', {'purchase': purchase, 'qr_code_base64': qr_code_base64, 'just_paid': just_paid})

ALLOWED_TICKET_EMAIL_IMAGE_TYPES = {'png', 'jpeg', 'jpg'}
MAX_TICKET_EMAIL_IMAGE_BYTES = 5 * 1024 * 1024

@csrf_exempt
def send_ticket_email(request):
    if request.method != 'POST':
        return HttpResponse(status=400)
    ip_address = request.META.get('REMOTE_ADDR')
    cache_key = f'send_ticket_email_{ip_address}'
    attempts = cache.get(cache_key, 0)
    if attempts >= 10:
        return HttpResponse(status=429)
    cache.set(cache_key, attempts + 1, 60)
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponse(status=400)
    image_data = data.get('image')
    reference = data.get('reference')
    purchase = TicketPurchase.objects.filter(paystack_reference=reference).first()
    if not (purchase and image_data):
        return HttpResponse(status=400)
    if purchase.buyer_email.endswith('@ussd.vote'):
        return HttpResponse(status=200)
    try:
        header, imgstr = image_data.split(';base64,')
        ext = header.split('/')[-1].lower()
        if ext not in ALLOWED_TICKET_EMAIL_IMAGE_TYPES:
            return HttpResponse(status=400)
        image_bytes = base64.b64decode(imgstr)
        if len(image_bytes) > MAX_TICKET_EMAIL_IMAGE_BYTES:
            return HttpResponse(status=400)
    except (ValueError, TypeError, base64.binascii.Error):
        return HttpResponse(status=400)
    email_subject = f"Your E-Ticket for {purchase.ticket.event.title}"
    email_body = f"Hi {purchase.buyer_name},\n\nThank you for your purchase! Please find your branded E-Ticket attached to this email. Present the QR code at the entrance for scanning.\n\nEvent: {purchase.ticket.event.title}\nTicket Type: {purchase.ticket.name}\nQuantity: {purchase.quantity}\nReference: {purchase.paystack_reference}\n\nSee you at the event!"
    email = EmailMessage(subject=email_subject, body=email_body, from_email=settings.DEFAULT_FROM_EMAIL, to=[purchase.buyer_email])
    email.attach(f"ticket_{purchase.paystack_reference}.{ext}", image_bytes, f'image/{ext}')
    email.send(fail_silently=True)
    return HttpResponse(status=200)

def verify_ticket_view(request):
    ticket_found = None
    error_message = None
    if request.method == 'POST':
        reference = request.POST.get('reference', '').strip().upper()
        purchase = TicketPurchase.objects.filter(paystack_reference=reference).first()
        if purchase:
            if purchase.status == 'Success':
                ticket_found = purchase
            else:
                error_message = "This ticket payment is still pending or failed."
        else:
            error_message = "Invalid reference code. No ticket found."
    return render(request, 'voting/verify_ticket.html', {'ticket_found': ticket_found, 'error_message': error_message})

@login_required(login_url='/login/')
def event_scanner(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event_id)
    return render(request, 'voting/scanner.html', {'event': event})

@login_required(login_url='/login/')
def process_scan(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.user != event.organizer and not request.user.is_staff:
        return JsonResponse({'status': 'error', 'message': 'Not authorized for this event.'}, status=403)
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({'status': 'error', 'message': 'Invalid request body.'}, status=400)
        scanned_text = data.get('text', '')
        reference = None
        for line in scanned_text.split('\n'):
            if line.startswith('REF:'):
                reference = line.replace('REF:', '').strip()
                break
        if not reference:
            return JsonResponse({'status': 'error', 'message': 'Invalid QR Code (No reference found).'}, status=400)
        purchase = TicketPurchase.objects.filter(event_id=event_id, paystack_reference=reference).first()
        if not purchase:
            return JsonResponse({'status': 'error', 'message': 'Ticket not found for this event.'}, status=404)
        if purchase.status != 'Success':
            return JsonResponse({'status': 'error', 'message': 'Payment pending or failed.'}, status=400)
        if purchase.is_checked_in:
            return JsonResponse({'status': 'error', 'message': f'ALREADY USED! Checked in at {purchase.checked_in_at.strftime("%I:%M %p")} by {purchase.buyer_name}.'}, status=409)
        purchase.is_checked_in = True
        purchase.checked_in_at = timezone.now()
        purchase.save()
        return JsonResponse({'status': 'success', 'message': f'Welcome, {purchase.buyer_name}! {purchase.quantity} {purchase.ticket.name} ticket(s).'})
    return JsonResponse({'status': 'error', 'message': 'Invalid request.'}, status=400)

@login_required(login_url='/login/')
def edit_ticket(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    event = ticket.event
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event.id)
    if request.method == 'POST':
        price, price_error = parse_non_negative_decimal(request.POST.get('price'), 'Price')
        old_price, old_price_error = parse_non_negative_decimal(request.POST.get('old_price'), 'Old price', required=False)
        quantity, quantity_error = parse_non_negative_int(request.POST.get('quantity_available'), 'Quantity available')
        error = price_error or old_price_error or quantity_error
        if error:
            messages.error(request, error)
            return render(request, 'voting/edit_ticket.html', {'ticket': ticket, 'event': event})
        ticket.name = request.POST.get('name')
        ticket.price = price
        ticket.old_price = old_price
        ticket.quantity_available = quantity
        ticket.is_active = request.POST.get('is_active') == 'on'
        if 'image' in request.FILES:
            ticket.image = request.FILES.get('image')
        ticket.save()
        messages.success(request, f'Ticket "{ticket.name}" updated successfully.')
        return redirect('event_detail', event_id=event.id)
    return render(request, 'voting/edit_ticket.html', {'ticket': ticket, 'event': event})

@login_required(login_url='/login/')
def delete_ticket(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    event = ticket.event
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event.id)
    if request.method == 'POST':
        ticket_name = ticket.name
        ticket.delete()
        messages.success(request, f'Ticket "{ticket_name}" deleted successfully.')
    return redirect('event_detail', event_id=event.id)

def retrieve_ticket_view(request):
    ticket_found = None
    error_message = None
    if request.method == 'POST':
        phone_or_ref = request.POST.get('phone_or_ref', '').strip()
        if phone_or_ref.upper().startswith('TK-'):
            purchase = TicketPurchase.objects.filter(paystack_reference=phone_or_ref.upper()).first()
        else:
            clean_phone = phone_or_ref.replace('+', '').replace(' ', '')
            purchase = TicketPurchase.objects.filter(buyer_email=f"{clean_phone}@ussd.vote").first()
        if purchase:
            if purchase.status == 'Success':
                return redirect(f"/ticket/success/?reference={purchase.paystack_reference}")
            else:
                error_message = "This ticket payment is still pending or failed."
        else:
            error_message = "No ticket found. Please check your Reference Code or Phone Number."
    return render(request, 'voting/retrieve_ticket.html', {'error_message': error_message})

def retrieve_voting_code(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.method == 'POST':
        student_id = request.POST.get('student_id', '').strip()
        email = request.POST.get('email', '').strip()
        voting_code = VotingCode.objects.filter(event=event, voter_identifier__iexact=student_id).first()
        if voting_code:
            if voting_code.is_used:
                messages.error(request, "This voting code has already been used to cast a vote.")
            else:
                subject = f"Your Voting Code for {event.title}"
                message = f"Hi {student_id},\n\nYour unique voting code for the event '{event.title}' is: {voting_code.code}\n\nPlease keep this secure and do not share it with anyone.\n\nThank you."
                try:
                    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=True)
                except Exception:
                    pass
                ActivityLog.objects.create(user=request.user if request.user.is_authenticated else None, event=event, action=f"Retrieved voting code for Student ID '{student_id}' sent to '{email}'")
                messages.success(request, f"Success! Your voting code has been sent to {email}.")
        else:
            messages.error(request, "No voting code found for the provided Student ID.")
    return redirect('event_detail', event_id=event_id)

@login_required(login_url='/login/')
def bulk_add_candidates(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event_id)
    if request.method == 'POST':
        names_text = request.POST.get('bulk_names', '')
        category_id = request.POST.get('bulk_category')
        category = Category.objects.get(id=category_id) if category_id else None
        names = [name.strip() for name in names_text.split('\n') if name.strip()]
        count = 0
        for name in names:
            Candidate.objects.create(event=event, name=name, category=category)
            count += 1
        if count > 0:
            ActivityLog.objects.create(user=request.user, event=event, action=f"Bulk added {count} candidates to '{event.title}'")
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'status': 'success', 'count': count})
        else:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'status': 'error', 'message': 'No valid names provided.'})
    return redirect('event_detail', event_id=event_id)

@login_required(login_url='/login/')
def upload_codes_csv(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.user != event.organizer and not request.user.is_staff:
        return redirect('event_detail', event_id=event_id)
    if request.method == 'POST':
        csv_file = request.FILES.get('csv_file')
        if not csv_file.name.endswith('.csv'):
            messages.error(request, 'Please upload a valid .csv file.')
            return redirect('event_detail', event_id=event_id)
        decoded_file = csv_file.read().decode('utf-8').splitlines()
        reader = csv.reader(decoded_file)
        imported_count = 0
        skipped_count = 0
        for row in reader:
            if row and row[0].strip():
                code = row[0].strip().upper()
                if not VotingCode.objects.filter(event=event, code=code).exists():
                    VotingCode.objects.create(event=event, code=code)
                    imported_count += 1
                else:
                    skipped_count += 1
        msg = f'{imported_count} codes imported successfully from CSV.'
        if skipped_count > 0:
            msg += f' ({skipped_count} duplicates skipped)'
        messages.success(request, msg)
    return redirect('event_detail', event_id=event_id)

# ==========================================
# DIGITAL BALLOT SYSTEM (AJAX)
# ==========================================

@csrf_exempt
def validate_ballot_code(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.method == 'POST':
        data = json.loads(request.body)
        code_input = data.get('code', '').strip().upper()
        identifier_input = data.get('identifier', '').strip()
        
        if timezone.now() > event.end_date:
            return JsonResponse({'status': 'error', 'message': 'Voting for this event has ended.'}, status=400)
            
        try:
            voting_code = VotingCode.objects.get(event=event, code=code_input)
            if voting_code.voter_identifier:
                if voting_code.voter_identifier.upper() != identifier_input.upper():
                    return JsonResponse({'status': 'error', 'message': 'The Student ID provided does not match this voting code.'}, status=400)
            if voting_code.is_used:
                return JsonResponse({'status': 'error', 'message': 'This code has already been used to vote.'}, status=400)
                
            categories_data = []
            for cat in event.categories.all():
                candidates_data = [{'id': c.id, 'name': c.name, 'image_url': c.image.url if c.image else ''} for c in cat.candidates.all()]
                categories_data.append({'id': cat.id, 'name': cat.name, 'candidates': candidates_data})
                
            uncategorized_candidates = [{'id': c.id, 'name': c.name, 'image_url': c.image.url if c.image else ''} for c in event.candidates.filter(category=None)]
            if uncategorized_candidates:
                categories_data.append({'id': 'none', 'name': 'General', 'candidates': uncategorized_candidates})

            return JsonResponse({'status': 'success', 'categories': categories_data})
        except VotingCode.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Invalid voting code. Please check and try again.'}, status=400)
    return JsonResponse({'status': 'error', 'message': 'Invalid request.'}, status=400)

@csrf_exempt
def cast_ballot(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.method == 'POST':
        data = json.loads(request.body)
        code_input = data.get('code', '').strip().upper()
        identifier_input = data.get('identifier', '').strip()
        votes = data.get('votes', {})
        
        if timezone.now() > event.end_date:
            return JsonResponse({'status': 'error', 'message': 'Voting has ended.'}, status=400)
            
        try:
            voting_code = VotingCode.objects.get(event=event, code=code_input)
            if voting_code.voter_identifier:
                if voting_code.voter_identifier.upper() != identifier_input.upper():
                    return JsonResponse({'status': 'error', 'message': 'Student ID mismatch.'}, status=400)
            if voting_code.is_used:
                return JsonResponse({'status': 'error', 'message': 'This code has already been used.'}, status=400)
                
            votes_cast = 0
            for cat_id, candidate_id in votes.items():
                if candidate_id:
                    try:
                        candidate = Candidate.objects.get(id=candidate_id, event=event)
                        VoteTransaction.objects.create(
                            candidate=candidate, voter_email=f"code_{code_input}@FlexyVotes.com", amount=0,
                            paystack_reference=f"CODE_{code_input}_{cat_id}_{uuid.uuid4().hex[:4].upper()}",
                            status='Success', vote_type='Main', number_of_votes=1
                        )
                        votes_cast += 1
                    except Candidate.DoesNotExist:
                        pass
            if votes_cast > 0:
                voting_code.is_used = True
                voting_code.used_at = timezone.now()
                voting_code.save()
            return JsonResponse({'status': 'success', 'message': f'Success! Your ballot has been cast. ({votes_cast} votes recorded).'})
        except VotingCode.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Invalid voting code.'}, status=400)
    return JsonResponse({'status': 'error', 'message': 'Invalid request.'}, status=400)