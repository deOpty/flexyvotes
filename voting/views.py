import base64
import csv
import io
import json
import zoneinfo
from datetime import datetime
from decimal import Decimal, InvalidOperation

import qrcode
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import IntegerField, Q, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core import audit, auth as staff_auth, captcha, crypto
from core.models import SupportTicket
from core.ratelimit import ratelimit
from core.rbac import check_perm, event_permission_required, has_perm, is_platform_admin
from core.tenancy import current_organization
from core.utils import client_ip, is_ajax, safe_next
from elections import lifecycle
from elections.ballot import positions_for

from .models import (Candidate, Category, Event, Product, ProductCategory, ProductImage, Profile, Ticket,
                     TicketPurchase, VoteTransaction)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def sanitize_csv_value(value):
    """Neutralise spreadsheet formula injection in exported CSV cells."""
    if isinstance(value, str) and value and value[0] in ('=', '+', '-', '@', '\t', '\r'):
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
    if not value.is_finite():
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


def parse_local_datetime(date_str, time_str, tz_name):
    """Interpret form date + time in the election's own time zone."""
    try:
        naive = datetime.strptime(f'{(date_str or "").strip()} {(time_str or "").strip()[:5]}', '%Y-%m-%d %H:%M')
    except ValueError:
        return None
    try:
        zone = zoneinfo.ZoneInfo(tz_name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        zone = zoneinfo.ZoneInfo(settings.TIME_ZONE)
    return timezone.make_aware(naive, zone)


def valid_timezone(name):
    try:
        zoneinfo.ZoneInfo(name)
        return True
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return False


def parse_ballot_rules(post_data):
    ballot_type = post_data.get('ballot_type') or Category.BallotType.SINGLE
    if ballot_type not in Category.BallotType.values:
        ballot_type = Category.BallotType.SINGLE

    def _int(name, default, minimum=0):
        try:
            return max(minimum, int(post_data.get(name, default)))
        except (TypeError, ValueError):
            return default

    min_select = _int('min_select', 1, 0)
    max_select = max(min_select, _int('max_select', 1, 1))
    seats = _int('seats', 1, 1)
    max_score = min(_int('max_score', 10, 1), 100)
    allow_abstain = post_data.get('allow_abstain', 'on') == 'on'
    threshold, _ = parse_non_negative_decimal(post_data.get('referendum_threshold') or '50', 'Threshold')
    threshold = min(threshold or Decimal('50'), Decimal('100'))
    return {'ballot_type': ballot_type, 'min_select': min_select, 'max_select': max_select, 'seats': seats,
            'max_score': max_score, 'allow_abstain': allow_abstain, 'referendum_threshold': threshold}


def _editable_or_message(request, event, scope):
    try:
        lifecycle.require_editable(event, scope, request.user, request)
        return True
    except lifecycle.LifecycleError as exc:
        messages.error(request, str(exc))
        return False


def _ajax_or_redirect(request, payload, event_id, status=200):
    if is_ajax(request):
        return JsonResponse(payload, status=status)
    if payload.get('status') == 'error':
        messages.error(request, payload.get('message', 'Something went wrong.'))
    return redirect('event_detail', event_id=event_id)


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------
def home(request):
    query = (request.GET.get('q') or '').strip()[:100]
    events = Event.objects.filter(is_active=True, status__in=Event.PUBLIC_STATUSES).order_by('-start_date')
    if query:
        events = events.filter(title__icontains=query)
    show_popup = request.session.pop('show_registration_popup', False)
    return render(request, 'voting/home.html', {'events': events[:60], 'show_popup': show_popup, 'query': query})


def event_detail(request, event_id):
    event = get_object_or_404(Event.objects.select_related('organization'), id=event_id)
    event = lifecycle.tick(event)
    can_manage = request.user.is_authenticated and has_perm(request.user, 'election.view', event)
    if not event.is_public and not can_manage:
        from django.http import Http404

        raise Http404
    positions = positions_for(event)
    counts = {}
    total_votes = 0
    show_counts = event.is_paid and (event.results_are_public or (
        can_manage and has_perm(request.user, 'vote.view', event)))
    if show_counts:
        from elections.results import paid_counts

        for row in paid_counts(event):
            counts[row['pk']] = row
            total_votes += row['main']
    candidates = list(event.candidates.select_related('category').all())
    for candidate in candidates:
        row = counts.get(candidate.pk)
        candidate.vote_count = row['main'] if row else 0
        candidate.tie_breaker_count = row['tie'] if row else 0
        candidate.percentage = int(candidate.vote_count * 100 / total_votes) if total_votes else 0
    turnout = None
    if event.is_institutional and (can_manage or event.status in lifecycle.LOCKED_STATES):
        from elections.voters import turnout as turnout_stats

        turnout = turnout_stats(event)
    return render(request, 'voting/event_detail.html', {
        'event': event, 'positions': positions, 'candidates': candidates,
        'uncategorized': [c for c in candidates if c.category_id is None],
        'show_counts': show_counts, 'total_votes': total_votes, 'can_manage': can_manage,
        'is_organizer_or_admin': can_manage, 'is_expired': timezone.now() > event.end_date,
        'accepting_votes': event.accepting_votes(), 'turnout': turnout,
        'packages': event.vote_packages.filter(is_active=True) if event.is_paid else [],
        'ballot_types': Category.BallotType.choices,
    })


def live_vote_counts(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    allowed = event.is_paid and (event.results_are_public or (
        request.user.is_authenticated and has_perm(request.user, 'vote.view', event)))
    if not allowed:
        return JsonResponse({'error': {'code': 'forbidden', 'message': 'Live counts are not public for this election.'}},
                            status=403)
    from elections.results import live_results

    data = live_results(event)
    candidates = []
    for position in data['positions']:
        for c in position['candidates']:
            candidates.append({'id': c['candidate_id'], 'name': c['name'], 'votes': c['votes'],
                               'tie_breakers': c.get('tie_breaker_votes', 0), 'percentage': int(c['percentage'])})
    response = JsonResponse({'candidates': candidates, 'total_votes': sum(c['votes'] for c in candidates),
                             'generated_at': data['generated_at']})
    if event.results_are_public:
        response['Cache-Control'] = 'public, max-age=5'
    return response


def contact_view(request):
    if request.method == 'POST':
        ok, _ = captcha.verify_human(request)
        name = (request.POST.get('name') or '').strip()[:150]
        email = (request.POST.get('email') or '').strip()
        subject = (request.POST.get('subject') or 'General enquiry').strip()[:200]
        message = (request.POST.get('message') or '').strip()[:5000]
        try:
            validate_email(email)
        except ValidationError:
            ok = False
        if not ok or not name or not message:
            messages.error(request, 'Please fill in your name, a valid email address and your message.')
            return render(request, 'voting/contact.html', {'form': request.POST})
        from core.ratelimit import hit

        allowed, _ = hit('contact', client_ip(request) or 'unknown', 5, 3600)
        if not allowed:
            messages.error(request, 'Too many messages. Please try again later.')
            return redirect('contact')
        ticket = SupportTicket.objects.create(requester_name=name, requester_email=email, subject=subject,
                                              category=(request.POST.get('category') or 'general')[:40])
        ticket.messages.create(body=message)
        from notifications.models import Notification
        from notifications.service import notify

        notify('support_ticket', channel=Notification.Channel.EMAIL, recipient=email,
               context={'reference': ticket.reference, 'subject': subject})
        messages.success(request, f'Your message has been received (reference {ticket.reference}). '
                                  'We will get back to you shortly.')
        return redirect('contact')
    return render(request, 'voting/contact.html')


# ---------------------------------------------------------------------------
# Legacy pay-to-vote endpoints (forward to the payments app)
# ---------------------------------------------------------------------------
def initiate_vote(request, candidate_id):
    candidate = get_object_or_404(Candidate, id=candidate_id)
    url = reverse('payments:pay', args=[candidate.event_id, candidate.pk])
    if request.method == 'POST':
        amount, _ = parse_non_negative_decimal(request.POST.get('amount'), 'Amount')
        price = candidate.event.vote_price if candidate.event.vote_price > 0 else Decimal('1')
        if amount:
            url += f'?votes={int(amount / price)}'
    return redirect(url)


def vote_success(request):
    reference = request.GET.get('reference') or request.GET.get('trxref') or ''
    return redirect(f"{reverse('payments:callback')}?reference={reference}")


def cast_vote_with_code(request, candidate_id):
    """Ticket-holder tie-breaker votes for paid events. Institutional code
    voting moved to the secret-ballot flow (/e/<id>/vote/)."""
    candidate = get_object_or_404(Candidate.objects.select_related('event'), id=candidate_id)
    event = candidate.event
    if request.method != 'POST':
        return redirect('event_detail', event_id=event.id)
    if event.is_institutional:
        return redirect('elections:vote_start', event_id=event.id)
    code_input = (request.POST.get('code') or '').strip().upper()
    if not event.accepting_votes():
        messages.error(request, 'Voting for this event is not open.')
        return redirect('event_detail', event_id=event.id)
    from core.ratelimit import hit

    allowed, _ = hit('ticket-vote', client_ip(request) or 'unknown', 20, 60)
    if not allowed:
        messages.error(request, 'Too many attempts. Please wait a minute and try again.')
        return redirect('event_detail', event_id=event.id)
    if not code_input.startswith('TK-') or not event.enable_tie_breaker:
        messages.error(request, 'Enter the reference printed on your ticket.')
        return redirect('event_detail', event_id=event.id)
    if candidate.status != Candidate.Status.ACTIVE:
        messages.error(request, f'{candidate.name} is no longer accepting votes.')
        return redirect('event_detail', event_id=event.id)
    with transaction.atomic():
        purchase = TicketPurchase.objects.select_for_update().filter(paystack_reference=code_input, event=event).first()
        if not purchase:
            messages.error(request, 'Invalid ticket reference. Please check your ticket.')
            return redirect('event_detail', event_id=event.id)
        if purchase.status != 'Success':
            messages.error(request, 'This ticket payment is still pending or failed.')
            return redirect('event_detail', event_id=event.id)
        if purchase.has_voted:
            messages.error(request, 'This ticket has already been used to vote.')
            return redirect('event_detail', event_id=event.id)
        if purchase.purchase_method != TicketPurchase.PurchaseMethod.WEB:
            messages.error(request, 'Only online tickets are eligible for the free vote.')
            return redirect('event_detail', event_id=event.id)
        VoteTransaction.objects.create(
            candidate=candidate, voter_email=f'ticket-vote@{event.id}.flexyvotes.internal', amount=0,
            paystack_reference=f'TB-{crypto.random_token(16)}', status=VoteTransaction.Status.SUCCESS,
            vote_type=VoteTransaction.VoteType.TIE_BREAKER, number_of_votes=purchase.quantity,
        )
        purchase.has_voted = True
        purchase.save(update_fields=['has_voted'])
    from django.core.cache import cache

    cache.delete(f'fv:live:{event.pk}')
    messages.success(request, f'Success! Your {purchase.quantity} free vote(s) for {candidate.name} have been cast.')
    return redirect('event_detail', event_id=event.id)


# ---------------------------------------------------------------------------
# Authentication (staff / organizers)
# ---------------------------------------------------------------------------
@ratelimit('login', 10, 60, key='ip')
def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    next_url = safe_next(request, request.POST.get('next') or request.GET.get('next'), reverse('dashboard'))
    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()[:150]
        # Only trim CR/LF, never spaces - a password pasted from a CRLF file
        # otherwise silently fails to match.
        password = (request.POST.get('password') or '').strip('\r\n')
        locked, until = staff_auth.is_locked(username)
        if locked:
            messages.error(request, 'This account is temporarily locked after too many failed attempts. '
                                    'Try again later or reset your password.')
            return render(request, 'voting/login.html', {'next': next_url}, status=429)
        from django.contrib.auth import authenticate

        user = authenticate(request, username=username, password=password)
        if user is None:
            staff_auth.register_failure(username, request)
            messages.error(request, 'Invalid username or password.')
            return render(request, 'voting/login.html', {'next': next_url})
        outcome = staff_auth.begin_login(request, user)
        if outcome == 'mfa':
            request.session['fv_login_next'] = next_url
            return redirect('account:mfa')
        return redirect(next_url)
    from core.sso import enabled_platform_providers

    return render(request, 'voting/login.html', {'next': next_url, 'sso_providers': enabled_platform_providers()})


@ratelimit('register', 5, 300, key='ip')
def register_view(request):
    if request.method == 'POST':
        ok, reason = captcha.verify_human(request)
        username = (request.POST.get('username') or '').strip()[:150]
        email = (request.POST.get('email') or '').strip()
        password = request.POST.get('password') or ''
        errors = []
        if not ok:
            errors.append('Please complete the verification and try again.')
        if not username or not username.replace('_', '').replace('.', '').replace('-', '').isalnum():
            errors.append('Choose a username using letters, numbers, dots, dashes or underscores.')
        elif User.objects.filter(username__iexact=username).exists():
            errors.append('Username already taken.')
        try:
            validate_email(email)
        except ValidationError:
            errors.append('Enter a valid email address.')
        if not password:
            errors.append('Password is required.')
        else:
            try:
                validate_password(password, user=User(username=username, email=email))
            except ValidationError as e:
                errors.extend(e.messages)
        if errors:
            for error in errors:
                messages.error(request, error)
            return render(request, 'voting/register.html', {'form': {'username': username, 'email': email}})
        user = User.objects.create_user(username=username, email=email, password=password)
        Profile.objects.create(user=user)
        audit.record('ORGANIZER_REGISTERED', request=request, actor=user, target=user,
                     summary=f'Organizer {username} registered (pending approval)')
        from notifications.service import notify_platform_admins

        notify_platform_admins('organizer_registered', {'username': username, 'email': email})
        staff_auth.complete_login(request, user, 'password')
        messages.success(request, 'Registration successful! Your organizer account is pending admin approval.')
        return redirect('home')
    return render(request, 'voting/register.html')


@require_POST
def logout_view(request):
    logout(request)
    return redirect('home')


@login_required
@require_POST
def approve_event(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    try:
        lifecycle.transition(event, 'approve', actor=request.user, request=request, reason='Approved from dashboard')
        messages.success(request, f'"{event.title}" has been approved. The organizer can now publish it.')
    except (lifecycle.LifecycleError, PermissionDenied) as exc:
        messages.error(request, str(exc) or 'You are not allowed to approve this election.')
    return redirect(safe_next(request, request.POST.get('next'), reverse('dashboard')))


# ---------------------------------------------------------------------------
# Election configuration (organizer tools)
# ---------------------------------------------------------------------------
def _event_form_values(request, event=None):
    """Validate the shared create/edit form. Returns (values, errors)."""
    errors = []
    tz_name = request.POST.get('timezone') or (event.timezone if event else settings.TIME_ZONE)
    if not valid_timezone(tz_name):
        errors.append('Choose a valid time zone.')
        tz_name = settings.TIME_ZONE
    start = parse_local_datetime(request.POST.get('start_date_date'), request.POST.get('start_date_time'), tz_name)
    end = parse_local_datetime(request.POST.get('end_date_date'), request.POST.get('end_date_time'), tz_name)
    if start is None or end is None:
        errors.append('Enter valid start and end dates and times.')
    elif end <= start:
        errors.append('The end date must be after the start date.')
    title = (request.POST.get('title') or '').strip()[:200]
    if not title:
        errors.append('Title is required.')
    voting_mode = request.POST.get('voting_mode') or Event.VotingMode.PAY_TO_VOTE
    if voting_mode not in Event.VotingMode.values:
        errors.append('Choose a voting mode.')
    currency = (request.POST.get('currency') or (event.currency if event else settings.DEFAULT_CURRENCY)).upper()
    if currency not in settings.SUPPORTED_CURRENCIES:
        errors.append('Unsupported currency.')
    values = {
        'title': title, 'description': (request.POST.get('description') or '').strip()[:5000],
        'voting_mode': voting_mode, 'timezone': tz_name, 'currency': currency, 'start_date': start, 'end_date': end,
        'code_voting_mode': request.POST.get('code_voting_mode') if request.POST.get('code_voting_mode') in
        Event.CodeVotingMode.values else Event.CodeVotingMode.STANDARD,
        'primary_color': (request.POST.get('primary_color') or '#800020')[:7],
        'accent_color': (request.POST.get('accent_color') or '#FFD700')[:7],
    }
    if voting_mode == Event.VotingMode.CODE_VOTING:
        values.update(platform_fee_percentage=Decimal('0'), vote_price=Decimal('0'), enable_tie_breaker=False)
    else:
        fee, fee_error = parse_non_negative_decimal(request.POST.get('platform_fee_percentage') or
                                                    (event.platform_fee_percentage if event else '20'), 'Platform fee percentage')
        price, price_error = parse_non_negative_decimal(request.POST.get('vote_price', '1.00'), 'Vote price')
        if fee is not None and fee > 100:
            fee_error = 'Platform fee percentage cannot exceed 100.'
        # Only platform staff set the platform's commission.
        if event is not None and not is_platform_admin(request.user):
            fee = event.platform_fee_percentage
            fee_error = None
        elif event is None and not is_platform_admin(request.user):
            fee, fee_error = Decimal('20.00'), None
        errors.extend(e for e in (fee_error, price_error) if e)
        values.update(platform_fee_percentage=fee, vote_price=price,
                      enable_tie_breaker=request.POST.get('enable_tie_breaker') == 'on')
    for color in ('primary_color', 'accent_color'):
        value = values[color]
        if not (len(value) == 7 and value.startswith('#') and all(c in '0123456789abcdefABCDEF' for c in value[1:])):
            values[color] = '#800020' if color == 'primary_color' else '#FFD700'
    return values, errors


@login_required
def create_event(request):
    organization = current_organization(request, 'election.create')
    if organization is None and not is_platform_admin(request.user):
        messages.error(request, 'Your organizer account is pending approval.')
        return redirect('home')
    context = {'timezones': sorted(zoneinfo.available_timezones()), 'currencies': settings.SUPPORTED_CURRENCIES,
               'default_timezone': organization.default_timezone if organization else settings.TIME_ZONE}
    if request.method == 'POST':
        check_perm(request.user, 'election.create', organization)
        values, errors = _event_form_values(request)
        if not errors and organization is not None:
            from billing.service import BillingLimitError, check_limit

            active = organization.events.exclude(status__in=[Event.Status.ARCHIVED]).count()
            try:
                check_limit(organization, 'max_active_elections', active + 1)
            except BillingLimitError as exc:
                errors.append(str(exc))
        if errors:
            for error in errors:
                messages.error(request, error)
            return render(request, 'voting/create_event.html', {**context, 'form': request.POST})
        institutional = values['voting_mode'] == Event.VotingMode.CODE_VOTING
        event = Event.objects.create(
            organizer=request.user, organization=organization, status=Event.Status.DRAFT,
            results_visibility=Event.ResultsVisibility.AFTER_PUBLISH if institutional else Event.ResultsVisibility.LIVE,
            dual_approval_required=institutional, background_image=request.FILES.get('background_image'),
            event_image=request.FILES.get('event_image'), **values,
        )
        audit.record('ELECTION_CREATED', request=request, event=event,
                     summary=f"Created election '{event.title}'", metadata={'mode': event.voting_mode})
        if organization is not None:
            from billing.service import record_usage

            record_usage(organization, 'ELECTION_CREATED', 1, event=event)
        messages.success(request, 'Election created as a draft. Add positions and candidates, then submit it for review.')
        return redirect('elections:console_overview', event_id=event.pk)
    return render(request, 'voting/create_event.html', context)


@event_permission_required('election.edit')
def edit_event(request, event):
    context = {'event': event, 'timezones': sorted(zoneinfo.available_timezones()),
               'currencies': settings.SUPPORTED_CURRENCIES}
    if request.method == 'POST':
        if not _editable_or_message(request, event, 'config'):
            return redirect('elections:console_overview', event_id=event.pk)
        values, errors = _event_form_values(request, event)
        if values['voting_mode'] != event.voting_mode and (event.voters.exists() or VoteTransaction.objects.filter(
                candidate__event=event).exists()):
            errors.append('The voting mode cannot change once voters or votes exist.')
        if errors:
            for error in errors:
                messages.error(request, error)
            return render(request, 'voting/edit_event.html', context)
        before = audit.snapshot(event, list(values) + ['background_image', 'event_image'])
        for name, value in values.items():
            setattr(event, name, value)
        if 'background_image' in request.FILES:
            event.background_image = request.FILES['background_image']
        if 'event_image' in request.FILES:
            event.event_image = request.FILES['event_image']
        event.save()
        audit.record('ELECTION_CONFIG_UPDATED', request=request, event=event, summary='Election details updated',
                     changes=audit.diff(before, audit.snapshot(event, list(values) + ['background_image', 'event_image'])))
        messages.success(request, 'Election details saved.')
        return redirect('elections:console_overview', event_id=event.pk)
    return render(request, 'voting/edit_event.html', context)


@event_permission_required('election.edit')
@require_POST
def add_category(request, event):
    if not _editable_or_message(request, event, 'ballot'):
        return _ajax_or_redirect(request, {'status': 'error', 'message': 'The ballot cannot be changed now.'}, event.pk, 409)
    name = (request.POST.get('name') or '').strip()[:100]
    if not name:
        return _ajax_or_redirect(request, {'status': 'error', 'message': 'Position name is required.'}, event.pk, 400)
    rules = parse_ballot_rules(request.POST)
    constituency = None
    if request.POST.get('constituency') and event.organization_id:
        from elections.models import Constituency

        constituency = Constituency.objects.filter(organization_id=event.organization_id,
                                                   pk=request.POST['constituency']).first()
    vote_price, _ = parse_non_negative_decimal(request.POST.get('vote_price'), 'Vote price', required=False)
    category = Category.objects.create(event=event, name=name, constituency=constituency,
                                       description=(request.POST.get('description') or '')[:2000],
                                       display_order=event.categories.count(), vote_price=vote_price, **rules)
    audit.record('ELECTION_POSITION_ADDED', request=request, event=event, target=category,
                 summary=f"Position '{name}' added", changes={'position': {'old': None, 'new': audit.snapshot(
                     category, ['name', 'ballot_type', 'min_select', 'max_select', 'seats', 'allow_abstain'])}})
    return _ajax_or_redirect(request, {'status': 'success', 'id': category.pk}, event.pk)


@login_required
def edit_category(request, category_id):
    category = get_object_or_404(Category.objects.select_related('event'), id=category_id)
    event = category.event
    check_perm(request.user, 'election.edit', event)
    if request.method == 'POST':
        if not _editable_or_message(request, event, 'ballot'):
            return redirect('elections:console_ballot', event_id=event.pk)
        fields = ['name', 'description', 'ballot_type', 'min_select', 'max_select', 'seats', 'max_score',
                  'allow_abstain', 'referendum_threshold', 'constituency', 'vote_price', 'max_votes_per_voter']
        before = audit.snapshot(category, fields)
        category.name = (request.POST.get('name') or category.name).strip()[:100]
        category.description = (request.POST.get('description') or '')[:2000]
        for key, value in parse_ballot_rules(request.POST).items():
            setattr(category, key, value)
        if event.organization_id:
            from elections.models import Constituency

            category.constituency = Constituency.objects.filter(
                organization_id=event.organization_id, pk=request.POST.get('constituency') or 0).first()
        category.vote_price, _ = parse_non_negative_decimal(request.POST.get('vote_price'), 'Vote price', required=False)
        category.max_votes_per_voter, _ = parse_non_negative_int(request.POST.get('max_votes_per_voter'), 'Limit', required=False)
        category.save()
        audit.record('ELECTION_POSITION_UPDATED', request=request, event=event, target=category,
                     summary=f"Position '{category.name}' updated", changes=audit.diff(before, audit.snapshot(category, fields)))
        messages.success(request, 'Position saved.')
        return redirect('elections:console_ballot', event_id=event.pk)
    constituencies = []
    if event.organization_id:
        from elections.models import Constituency

        constituencies = Constituency.objects.filter(organization_id=event.organization_id)
    return render(request, 'voting/edit_category.html', {'category': category, 'event': event,
                                                         'ballot_types': Category.BallotType.choices,
                                                         'constituencies': constituencies})


@login_required
@require_POST
def delete_category(request, category_id):
    category = get_object_or_404(Category.objects.select_related('event'), id=category_id)
    event = category.event
    check_perm(request.user, 'election.edit', event)
    if _editable_or_message(request, event, 'ballot'):
        if VoteTransaction.objects.filter(candidate__category=category).exists():
            messages.error(request, 'This position already has votes and cannot be deleted.')
        else:
            audit.record('ELECTION_POSITION_DELETED', request=request, event=event, target=category,
                         summary=f"Position '{category.name}' deleted")
            category.delete()
            messages.success(request, 'Position deleted.')
    return redirect('elections:console_ballot', event_id=event.pk)


def _candidate_from_post(request, event, candidate=None):
    category_id = request.POST.get('category')
    category = event.categories.filter(pk=category_id).first() if category_id else None
    if category_id and category is None:
        return None, 'Invalid position.'
    name = (request.POST.get('name') or '').strip()[:100]
    if not name:
        return None, 'Candidate name is required.'
    nominee_code = (request.POST.get('nominee_code') or '').strip().upper()[:10] or None
    if nominee_code and Candidate.objects.filter(nominee_code=nominee_code).exclude(pk=getattr(candidate, 'pk', None)).exists():
        return None, 'That nominee code is already in use.'
    image = request.FILES.get('image')
    if image is not None:
        from core.storage import validate_document

        error = validate_document(image, max_bytes=2 * 1024 * 1024,
                                  allowed={'.png': b'\x89PNG', '.jpg': b'\xff\xd8\xff', '.jpeg': b'\xff\xd8\xff'})
        if error:
            return None, error
    return {'category': category, 'name': name, 'nominee_code': nominee_code, 'image': image,
            'bio': (request.POST.get('bio') or '')[:5000], 'manifesto': (request.POST.get('manifesto') or '')[:20000],
            'affiliation': (request.POST.get('affiliation') or '')[:120],
            'email': (request.POST.get('email') or '').strip()[:254]}, None


@event_permission_required('candidate.create')
@require_POST
def add_candidate(request, event):
    if not _editable_or_message(request, event, 'candidates'):
        return _ajax_or_redirect(request, {'status': 'error', 'message': 'Candidates are frozen.'}, event.pk, 409)
    values, error = _candidate_from_post(request, event)
    if error:
        return _ajax_or_redirect(request, {'status': 'error', 'message': error}, event.pk, 400)
    image = values.pop('image')
    candidate = Candidate.objects.create(event=event, image=image, display_order=event.candidates.count(), **values)
    audit.record('ELECTION_CANDIDATE_ADDED', request=request, event=event, target=candidate,
                 summary=f"Candidate '{candidate.name}' added")
    return _ajax_or_redirect(request, {'status': 'success', 'id': candidate.pk}, event.pk)


@login_required
def edit_candidate(request, candidate_id):
    candidate = get_object_or_404(Candidate.objects.select_related('event'), id=candidate_id)
    event = candidate.event
    check_perm(request.user, 'candidate.edit', event)
    if request.method == 'POST':
        structural = str(candidate.category_id or '') != (request.POST.get('category') or '') or \
            (request.POST.get('status') and request.POST.get('status') != candidate.status)
        if not _editable_or_message(request, event, 'candidates' if structural else 'candidate_profile'):
            return redirect('elections:console_ballot', event_id=event.pk)
        values, error = _candidate_from_post(request, event, candidate)
        if error:
            messages.error(request, error)
            return redirect('edit_candidate', candidate_id=candidate.pk)
        fields = ['name', 'bio', 'manifesto', 'affiliation', 'nominee_code', 'category', 'status', 'email', 'image']
        before = audit.snapshot(candidate, fields)
        image = values.pop('image')
        for name, value in values.items():
            setattr(candidate, name, value)
        if image is not None:
            candidate.image = image
        status = request.POST.get('status')
        if status in Candidate.Status.values:
            candidate.status = status
        candidate.save()
        audit.record('ELECTION_CANDIDATE_UPDATED', request=request, event=event, target=candidate,
                     summary=f"Candidate '{candidate.name}' updated", changes=audit.diff(before, audit.snapshot(candidate, fields)))
        messages.success(request, 'Candidate saved.')
        return redirect('elections:console_ballot', event_id=event.pk)
    return render(request, 'voting/edit_candidate.html', {'candidate': candidate, 'event': event,
                                                          'statuses': Candidate.Status.choices})


@event_permission_required('candidate.create')
@require_POST
def bulk_add_candidates(request, event):
    if not _editable_or_message(request, event, 'candidates'):
        return _ajax_or_redirect(request, {'status': 'error', 'message': 'Candidates are frozen.'}, event.pk, 409)
    category_id = request.POST.get('bulk_category')
    category = event.categories.filter(pk=category_id).first() if category_id else None
    names = [n.strip()[:100] for n in (request.POST.get('bulk_names') or '').splitlines() if n.strip()][:500]
    if not names:
        return _ajax_or_redirect(request, {'status': 'error', 'message': 'No valid names provided.'}, event.pk, 400)
    order = event.candidates.count()
    for offset, name in enumerate(names):
        Candidate.objects.create(event=event, name=name, category=category, display_order=order + offset)
    audit.record('ELECTION_CANDIDATES_BULK_ADDED', request=request, event=event,
                 summary=f'{len(names)} candidates added', metadata={'position': category.pk if category else None})
    return _ajax_or_redirect(request, {'status': 'success', 'count': len(names)}, event.pk)


@event_permission_required('vote.view')
def event_analytics(request, event):
    if event.is_institutional:
        # Secret ballot: only aggregate participation before certification.
        from elections.voters import turnout

        return render(request, 'voting/analytics.html', {'event': event, 'turnout': turnout(event), 'chart_data': [],
                                                         'all_candidates': []})
    chart_data = []
    for category in event.categories.all():
        candidates = category.candidates.annotate(
            main_votes=Coalesce(Sum('transactions__number_of_votes', filter=Q(transactions__status='Success', transactions__vote_type='Main')), 0, output_field=IntegerField()),
            tie_breaker_votes=Coalesce(Sum('transactions__number_of_votes', filter=Q(transactions__status='Success', transactions__vote_type='Tie-Breaker')), 0, output_field=IntegerField()),
        ).order_by('-main_votes', '-tie_breaker_votes')
        chart_data.append({'category_name': category.name, 'labels': [c.name for c in candidates],
                           'main_data': [c.main_votes for c in candidates],
                           'tie_breaker_data': [c.tie_breaker_votes for c in candidates]})
    from django.db.models import DecimalField

    all_candidates = event.candidates.annotate(
        main_votes=Coalesce(Sum('transactions__number_of_votes', filter=Q(transactions__status='Success', transactions__vote_type='Main')), 0, output_field=IntegerField()),
        tie_breaker_votes=Coalesce(Sum('transactions__number_of_votes', filter=Q(transactions__status='Success', transactions__vote_type='Tie-Breaker')), 0, output_field=IntegerField()),
        revenue=Coalesce(Sum('transactions__amount', filter=Q(transactions__status='Success')), Decimal('0'), output_field=DecimalField()),
    ).order_by('category__name', '-main_votes')
    return render(request, 'voting/analytics.html', {'event': event, 'chart_data': chart_data,
                                                     'all_candidates': all_candidates})


# ---------------------------------------------------------------------------
# Legacy code-voting management URLs (now backed by the voter roll)
# ---------------------------------------------------------------------------
@event_permission_required('voter.credentials')
@require_POST
def generate_codes(request, event):
    from elections import voters as voter_service

    try:
        identifiers = (request.POST.get('identifiers') or '').strip()
        if identifiers:
            rows = []
            for line in identifiers.splitlines():
                identifier, _, email = line.partition(',')
                if identifier.strip():
                    rows.append({'identifier': identifier.strip(), 'email': email.strip() or None, 'attributes': {}})
            report = voter_service.import_voters(event, rows, request.user, request=request)
            messages.success(request, f'{report.created} voters added ({report.codes_issued} codes issued).')
            for error in report.errors[:5]:
                messages.error(request, f"Row {error['row']}: {error['error']}")
        else:
            count, count_error = parse_non_negative_int(request.POST.get('count', '10'), 'Count')
            if count_error:
                messages.error(request, count_error)
            else:
                created = voter_service.generate_anonymous_codes(event, min(count, 500), request.user, request)
                messages.success(request, f'{created} access codes generated.')
    except (lifecycle.LifecycleError, voter_service.VoterImportError, Exception) as exc:  # noqa: BLE001
        if isinstance(exc, PermissionDenied):
            raise
        messages.error(request, str(exc))
    return redirect('elections:console_voters', event_id=event.pk)


@event_permission_required('voter.import')
@require_POST
def upload_student_csv(request, event):
    from elections import voters as voter_service

    uploaded = request.FILES.get('csv_file')
    if uploaded is None:
        messages.error(request, 'Choose a CSV or Excel file to upload.')
        return redirect('elections:console_voters', event_id=event.pk)
    try:
        rows = voter_service.parse_upload(uploaded)
        source = 'XLSX' if uploaded.name.lower().endswith(('.xlsx', '.xlsm')) else 'CSV'
        report = voter_service.import_voters(event, rows, request.user, source=source, request=request)
        messages.success(request, f'Import complete: {report.created} added, {report.updated} updated, '
                                  f'{report.skipped} skipped, {len(report.errors)} errors.')
        for error in report.errors[:10]:
            messages.error(request, f"Row {error['row']}: {error['error']}")
    except (voter_service.VoterImportError, lifecycle.LifecycleError) as exc:
        messages.error(request, str(exc))
    except Exception as exc:  # noqa: BLE001 - billing limit etc.
        if isinstance(exc, PermissionDenied):
            raise
        messages.error(request, str(exc))
    return redirect('elections:console_voters', event_id=event.pk)


@event_permission_required('voter.credentials')
@require_POST
def upload_codes_csv(request, event):
    """Import pre-printed access codes (one per row, optional voter ID column)."""
    from django.db import IntegrityError

    from elections.models import Voter

    uploaded = request.FILES.get('csv_file')
    if uploaded is None or not uploaded.name.lower().endswith('.csv'):
        messages.error(request, 'Please upload a valid .csv file.')
        return redirect('elections:console_voters', event_id=event.pk)
    if not _editable_or_message(request, event, 'voters'):
        return redirect('elections:console_voters', event_id=event.pk)
    imported = skipped = 0
    text = uploaded.read().decode('utf-8-sig', errors='replace')
    for row in csv.reader(io.StringIO(text)):
        code = (row[0] if row else '').strip().upper()
        if not code or len(code) < 6 or not code.isalnum():
            skipped += 1
            continue
        voter = Voter(election=event, source=Voter.Source.CODES,
                      identifier=(row[1].strip().upper() if len(row) > 1 and row[1].strip() else None))
        voter.set_credential(code)
        try:
            with transaction.atomic():
                voter.save()
            imported += 1
        except IntegrityError:
            skipped += 1
    audit.record('VOTER_CODES_IMPORTED', request=request, event=event, summary=f'{imported} pre-made codes imported')
    messages.success(request, f'{imported} codes imported.' + (f' ({skipped} duplicates/invalid skipped)' if skipped else ''))
    return redirect('elections:console_voters', event_id=event.pk)


@event_permission_required('voter.credentials')
def download_codes(request, event):
    return redirect('elections:console_voters_export', event_id=event.pk)


@event_permission_required('voter.credentials')
@require_POST
def clear_codes(request, event):
    from elections import voters as voter_service

    try:
        count = voter_service.clear_unvoted(event, request.user, request)
        messages.success(request, f'{count} unused voter credentials removed.')
    except lifecycle.LifecycleError as exc:
        messages.error(request, str(exc))
    return redirect('elections:console_voters', event_id=event.pk)


@event_permission_required('election.pause')
@require_POST
def toggle_voting_lock(request, event):
    action = 'resume' if event.status == Event.Status.PAUSED else 'pause'
    try:
        lifecycle.transition(event, action, actor=request.user, request=request, reason='Toggled from election page')
        messages.success(request, 'Voting resumed.' if action == 'resume' else 'Voting paused.')
    except lifecycle.LifecycleError as exc:
        messages.error(request, str(exc))
    return redirect('elections:console_overview', event_id=event.pk)


@ratelimit('retrieve-code', 5, 60, key='ip')
def retrieve_voting_code(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    if request.method == 'POST':
        from elections.voters import resend_credential_public

        resend_credential_public(event, request.POST.get('student_id', ''), request)
        messages.success(request, 'If that ID is registered with an unused access code, a new code has been sent to '
                                  'the email address on the voter roll.')
    return redirect('elections:vote_start', event_id=event.id)


# ---------------------------------------------------------------------------
# Store (platform merchandise)
# ---------------------------------------------------------------------------
def store_view(request):
    categories = ProductCategory.objects.all()
    selected_category = request.GET.get('category')
    products = Product.objects.filter(is_active=True).order_by('-created_at')
    if selected_category:
        products = products.filter(category__name=selected_category)
    return render(request, 'voting/store.html', {'products': products, 'categories': categories,
                                                 'selected_category': selected_category})


def _store_admin(request):
    if not has_perm(request.user, 'store.manage'):
        raise PermissionDenied


@login_required
def manage_store(request):
    _store_admin(request)
    return render(request, 'voting/manage_store.html', {'products': Product.objects.all().order_by('-created_at'),
                                                        'categories': ProductCategory.objects.all()})


def _product_from_post(request, product=None):
    price, price_error = parse_non_negative_decimal(request.POST.get('price'), 'Price')
    old_price, old_error = parse_non_negative_decimal(request.POST.get('old_price'), 'Old price', required=False)
    error = price_error or old_error
    if not (request.POST.get('name') or '').strip():
        error = error or 'Name is required.'
    category_id = request.POST.get('category')
    category = ProductCategory.objects.filter(id=category_id).first() if category_id else None
    return {'name': (request.POST.get('name') or '').strip()[:200], 'description': request.POST.get('description', ''),
            'price': price, 'old_price': old_price, 'category': category,
            'is_active': request.POST.get('is_active') == 'on'}, error


@login_required
def add_product(request):
    _store_admin(request)
    if request.method == 'POST':
        values, error = _product_from_post(request)
        if error:
            messages.error(request, error)
            return render(request, 'voting/add_product.html', {'categories': ProductCategory.objects.all()})
        product = Product.objects.create(image=request.FILES.get('image'), **values)
        for img in request.FILES.getlist('additional_images')[:10]:
            ProductImage.objects.create(product=product, image=img)
        audit.record('STORE_PRODUCT_ADDED', request=request, target=product, summary=f'Product {product.name} added')
        return redirect('manage_store')
    return render(request, 'voting/add_product.html', {'categories': ProductCategory.objects.all()})


@login_required
def edit_product(request, product_id):
    _store_admin(request)
    product = get_object_or_404(Product, id=product_id)
    if request.method == 'POST':
        values, error = _product_from_post(request, product)
        if error:
            messages.error(request, error)
            return render(request, 'voting/edit_product.html', {'product': product, 'categories': ProductCategory.objects.all()})
        for name, value in values.items():
            setattr(product, name, value)
        if 'image' in request.FILES:
            product.image = request.FILES['image']
        product.save()
        for img in request.FILES.getlist('additional_images')[:10]:
            ProductImage.objects.create(product=product, image=img)
        audit.record('STORE_PRODUCT_UPDATED', request=request, target=product, summary=f'Product {product.name} updated')
        return redirect('manage_store')
    return render(request, 'voting/edit_product.html', {'product': product, 'categories': ProductCategory.objects.all()})


@login_required
@require_POST
def add_product_category(request):
    _store_admin(request)
    name = (request.POST.get('name') or '').strip()[:100]
    if name:
        ProductCategory.objects.create(name=name)
        messages.success(request, f'Category "{name}" added successfully.')
    return redirect('manage_store')


# ---------------------------------------------------------------------------
# USSD (Africa's Talking)
# ---------------------------------------------------------------------------
def _ussd(text):
    return HttpResponse(text, content_type='text/plain')


def _ussd_authorized(request):
    token = settings.USSD_CALLBACK_TOKEN
    if token and not crypto.constant_time_equals(request.GET.get('token', ''), token):
        return False
    if settings.USSD_ALLOWED_IPS and client_ip(request) not in settings.USSD_ALLOWED_IPS:
        return False
    return True


def _ussd_events():
    return Event.objects.filter(is_active=True, status__in=Event.PUBLIC_STATUSES, tickets__is_active=True).distinct().order_by('id')


@csrf_exempt
@require_POST
def ussd_callback(request):
    """Africa's Talking USSD. Payments are real mobile-money charges via
    Paystack - nothing is credited until Paystack confirms the charge."""
    if not _ussd_authorized(request):
        audit.record('USSD_REJECTED', request=request, result='DENIED', summary='USSD callback failed authentication')
        return HttpResponse(status=403)
    phone_number = (request.POST.get('phoneNumber') or '')[:20]
    text = (request.POST.get('text') or '')[:200]
    inputs = text.split('*') if text else []
    if not inputs:
        return _ussd('CON Welcome to FlexyVotes.\n1. Vote for Candidate\n2. Buy Event Ticket')
    if inputs[0] == '1':
        return _ussd_vote(request, inputs, phone_number)
    if inputs[0] == '2':
        return _ussd_ticket(request, inputs, phone_number)
    return _ussd('END Invalid request.')


def _ussd_vote(request, inputs, phone_number):
    from payments import paystack
    from payments.service import PaymentError, initiate_vote_payment

    if len(inputs) == 1:
        return _ussd('CON Enter Nominee Code:')
    candidate = Candidate.objects.select_related('event').filter(nominee_code=inputs[1].strip().upper()).first()
    if candidate is None or not candidate.event.is_paid or not candidate.event.accepting_votes():
        return _ussd('END Invalid nominee code or voting is closed.')
    event = candidate.event
    if len(inputs) == 2:
        return _ussd(f'CON You selected {candidate.name}.\nEnter number of votes ({event.currency} {event.vote_price} each):')
    try:
        votes = int(inputs[2])
    except ValueError:
        return _ussd('END Invalid input. Please enter a number.')
    if votes < 1:
        return _ussd('END Invalid number of votes.')
    total = (event.vote_price * votes).quantize(Decimal('0.01'))
    if len(inputs) == 3:
        return _ussd(f'CON Pay {event.currency} {total} for {votes} votes for {candidate.name}?\n1. Confirm\n2. Cancel')
    if inputs[3] != '1':
        return _ussd('END Transaction cancelled.')
    provider, local = paystack.ghana_momo_provider(phone_number)
    if provider is None:
        return _ussd('END Mobile money is not supported for this number.')
    session_id = (request.POST.get('sessionId') or '')[:100]
    try:
        payment = initiate_vote_payment(request, event, candidate, votes=votes, phone=phone_number,
                                        idempotency_key=f'ussd:{session_id}' if session_id else None,
                                        channel_hint='mobile_money',
                                        mobile_money={'phone': local, 'provider': provider})
    except PaymentError as exc:
        return _ussd(f'END {exc.message[:120]}')
    return _ussd(f'END Approve the {event.currency} {total} prompt on your phone. Your {votes} votes for '
                 f'{candidate.name} count once payment is confirmed. Ref: {payment.reference}')


def _ussd_ticket(request, inputs, phone_number):
    from payments import paystack

    events = list(_ussd_events())
    if len(inputs) == 1:
        if not events:
            return _ussd('END No events are selling tickets right now.')
        return _ussd('CON Select Event:\n' + '\n'.join(f'{i + 1}. {e.title}' for i, e in enumerate(events)))
    try:
        selected_event = events[int(inputs[1]) - 1]
    except (ValueError, IndexError):
        return _ussd('END Invalid event selected.')
    tickets = list(selected_event.tickets.filter(is_active=True).order_by('id'))
    if len(inputs) == 2:
        if not tickets:
            return _ussd('END No tickets available for this event.')
        return _ussd(f'CON Select Ticket for {selected_event.title}:\n' +
                     '\n'.join(f'{i + 1}. {t.name} - {selected_event.currency} {t.price}' for i, t in enumerate(tickets)))
    try:
        ticket = tickets[int(inputs[2]) - 1]
    except (ValueError, IndexError):
        return _ussd('END Invalid ticket selected.')
    if len(inputs) == 3:
        return _ussd(f'CON You selected {ticket.name} ({selected_event.currency} {ticket.price}).\nEnter Quantity:')
    try:
        quantity = int(inputs[3])
    except ValueError:
        return _ussd('END Invalid quantity.')
    if quantity < 1 or quantity > 20:
        return _ussd('END Invalid quantity.')
    total = ticket.price * quantity
    if len(inputs) == 4:
        return _ussd(f'CON Total: {selected_event.currency} {total} for {quantity} {ticket.name}.\nEnter your full name:')
    buyer_name = inputs[4].strip()[:150]
    if len(inputs) == 5:
        return _ussd(f'CON Pay {selected_event.currency} {total} for {quantity} {ticket.name} for {buyer_name}?\n1. Confirm\n2. Cancel')
    if inputs[5] != '1':
        return _ussd('END Transaction cancelled.')
    provider, local = paystack.ghana_momo_provider(phone_number)
    if provider is None or not paystack.configured():
        return _ussd('END Mobile money payment is unavailable for this number.')
    with transaction.atomic():
        locked = Ticket.objects.select_for_update().get(pk=ticket.pk)
        if locked.reserved_count() + quantity > locked.quantity_available:
            return _ussd('END Sorry, not enough tickets available for this request.')
        reference = f'TK-{crypto.access_code(10)}'
        clean_phone = phone_number.replace('+', '').replace(' ', '')
        TicketPurchase.objects.create(ticket=locked, event=selected_event, buyer_name=buyer_name,
                                      buyer_email=f'{clean_phone}@ussd.vote', quantity=quantity,
                                      paystack_reference=reference, status='Pending',
                                      purchase_method=TicketPurchase.PurchaseMethod.USSD)
    try:
        paystack.charge_mobile_money(email=f'{reference.lower()}@payments.flexyvotes.invalid',
                                     amount_minor=int(total * 100), currency=selected_event.currency, phone=local,
                                     provider=provider, reference=reference, metadata={'type': 'ticket_purchase'})
    except paystack.PaystackError:
        TicketPurchase.objects.filter(paystack_reference=reference).update(status='Failed')
        return _ussd('END Could not start the mobile money payment. Please try again.')
    return _ussd(f'END Approve the {selected_event.currency} {total} prompt on your phone.\n'
                 f'Ref: {reference}\nVisit FlexyVotes.com/retrieve-ticket after payment.')


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------
@event_permission_required('ticket.manage')
def create_ticket(request, event):
    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()[:100]
        price, price_error = parse_non_negative_decimal(request.POST.get('price'), 'Price')
        old_price, old_price_error = parse_non_negative_decimal(request.POST.get('old_price'), 'Old price', required=False)
        quantity, quantity_error = parse_non_negative_int(request.POST.get('quantity_available'), 'Quantity available')
        error = price_error or old_price_error or quantity_error or (None if name else 'Ticket name is required.')
        if error:
            messages.error(request, error)
            return render(request, 'voting/create_ticket.html', {'event': event})
        ticket = Ticket.objects.create(event=event, name=name, price=price, old_price=old_price,
                                       quantity_available=quantity, image=request.FILES.get('image'))
        audit.record('TICKET_TYPE_CREATED', request=request, event=event, target=ticket, summary=f'Ticket "{name}" created')
        messages.success(request, f'Ticket type "{name}" added successfully.')
        return redirect('event_detail', event_id=event.pk)
    return render(request, 'voting/create_ticket.html', {'event': event})


@ratelimit('buy-ticket', 10, 60, key='ip')
def buy_ticket(request, ticket_id):
    from payments import paystack

    if request.method != 'POST':
        return redirect('tickets')
    ticket = get_object_or_404(Ticket.objects.select_related('event'), id=ticket_id, is_active=True)
    buyer_name = (request.POST.get('name') or '').strip()[:150]
    buyer_email = (request.POST.get('email') or '').strip()
    quantity, quantity_error = parse_non_negative_int(request.POST.get('quantity', 1), 'Quantity')
    try:
        validate_email(buyer_email)
    except ValidationError:
        quantity_error = quantity_error or 'Enter a valid email address.'
    if quantity_error or not quantity or quantity < 1 or quantity > 50:
        messages.error(request, quantity_error or 'Please enter a valid quantity (1-50).')
        return redirect('event_tickets', event_id=ticket.event.id)
    if not paystack.configured():
        messages.error(request, 'Ticket payments are unavailable right now.')
        return redirect('event_tickets', event_id=ticket.event.id)
    reference = f'TK-{crypto.access_code(10)}'
    with transaction.atomic():
        locked = Ticket.objects.select_for_update().get(pk=ticket.pk)
        if locked.reserved_count() + quantity > locked.quantity_available:
            messages.error(request, 'Not enough tickets available for this request.')
            return redirect('event_tickets', event_id=ticket.event.id)
        TicketPurchase.objects.create(ticket=locked, event=ticket.event, buyer_name=buyer_name, buyer_email=buyer_email,
                                      quantity=quantity, paystack_reference=reference, status='Pending')
    try:
        data = paystack.initialize(email=buyer_email, amount_minor=int(ticket.price * quantity * 100),
                                   currency=ticket.event.currency, reference=reference,
                                   callback_url=f'{settings.SITE_URL}/ticket/success/',
                                   metadata={'type': 'ticket_purchase', 'ticket_id': ticket.pk, 'quantity': quantity})
    except paystack.PaystackError as exc:
        TicketPurchase.objects.filter(paystack_reference=reference).update(status='Failed')
        messages.error(request, f'Could not initialize payment: {exc}')
        return redirect('event_tickets', event_id=ticket.event.id)
    return redirect(data['authorization_url'])


def tickets_view(request):
    events_with_tickets = Event.objects.filter(is_active=True, status__in=Event.PUBLIC_STATUSES,
                                               tickets__isnull=False).distinct().order_by('-start_date')
    ticket_found = error_message = active_action = None
    if request.method == 'POST':
        from core.ratelimit import hit

        allowed, _ = hit('ticket-lookup', client_ip(request) or 'unknown', 20, 60)
        action = request.POST.get('action')
        active_action = action
        if not allowed:
            error_message = 'Too many lookups. Please wait a minute.'
        elif action == 'verify':
            purchase = TicketPurchase.objects.filter(paystack_reference=(request.POST.get('reference') or '').strip().upper()).first()
            if purchase and purchase.status == 'Success':
                ticket_found = purchase
            else:
                error_message = 'No paid ticket found for that reference.'
        elif action == 'retrieve':
            return retrieve_ticket_view(request)
    return render(request, 'voting/tickets.html', {'events': events_with_tickets, 'ticket_found': ticket_found,
                                                   'error_message': error_message, 'active_action': active_action})


def event_tickets_view(request, event_id):
    event = get_object_or_404(Event, id=event_id)
    return render(request, 'voting/event_tickets.html', {'event': event, 'tickets': event.tickets.filter(is_active=True)})


@event_permission_required('ticket.manage')
def event_guestlist(request, event):
    purchases = TicketPurchase.objects.filter(event=event, status='Success').select_related('ticket').order_by('-purchased_at')
    return render(request, 'voting/guestlist.html', {'event': event, 'purchases': purchases})


@event_permission_required('ticket.manage')
def download_guestlist(request, event):
    purchases = TicketPurchase.objects.filter(event=event, status='Success').select_related('ticket').order_by('-purchased_at')
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="guestlist_{event.pk}.csv"'
    writer = csv.writer(response)
    writer.writerow(['Buyer Name', 'Buyer Email', 'Ticket Type', 'Quantity', 'Reference', 'Purchased At'])
    for p in purchases:
        writer.writerow([sanitize_csv_value(p.buyer_name), sanitize_csv_value(p.buyer_email), sanitize_csv_value(p.ticket.name),
                         p.quantity, sanitize_csv_value(p.paystack_reference), p.purchased_at])
    audit.record('GUESTLIST_EXPORTED', request=request, event=event, summary='Guest list exported')
    return response


def ticket_success(request):
    from payments.service import verify_and_apply

    reference = (request.GET.get('reference') or request.GET.get('trxref') or '').strip()
    purchase = TicketPurchase.objects.select_related('ticket', 'ticket__event').filter(paystack_reference=reference).first()
    if purchase is None:
        return redirect('home')
    just_paid = False
    if purchase.status == 'Pending':
        verify_and_apply(reference, 'CALLBACK')
        purchase.refresh_from_db()
        just_paid = purchase.status == 'Success'
        if not just_paid:
            messages.error(request, 'We could not confirm your payment yet. If you were charged, your ticket will be confirmed shortly.')
            return redirect('tickets')
    if purchase.status != 'Success':
        messages.error(request, 'This ticket payment was not completed.')
        return redirect('tickets')
    qr_data = (f'EVENT: {purchase.ticket.event.title}\nNAME: {purchase.buyer_name}\nTICKET: {purchase.ticket.name}\n'
               f'QTY: {purchase.quantity}\nREF: {purchase.paystack_reference}')
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(qr_data)
    qr.make(fit=True)
    buffer = io.BytesIO()
    qr.make_image(fill_color='black', back_color='white').save(buffer, format='PNG')
    return render(request, 'voting/ticket_success.html', {
        'purchase': purchase, 'qr_code_base64': base64.b64encode(buffer.getvalue()).decode('utf-8'),
        'just_paid': just_paid})


ALLOWED_TICKET_EMAIL_IMAGE_TYPES = {'png', 'jpeg', 'jpg'}
MAX_TICKET_EMAIL_IMAGE_BYTES = 5 * 1024 * 1024


@require_POST
@ratelimit('ticket-email', 10, 60, key='ip')
def send_ticket_email(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponse(status=400)
    image_data = data.get('image') or ''
    purchase = TicketPurchase.objects.select_related('ticket', 'ticket__event').filter(
        paystack_reference=str(data.get('reference') or '')[:100], status='Success').first()
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
    email = EmailMessage(
        subject=f'Your E-Ticket for {purchase.ticket.event.title}',
        body=(f'Hi {purchase.buyer_name},\n\nThank you for your purchase! Your E-Ticket is attached. Present the QR '
              f'code at the entrance.\n\nEvent: {purchase.ticket.event.title}\nTicket Type: {purchase.ticket.name}\n'
              f'Quantity: {purchase.quantity}\nReference: {purchase.paystack_reference}\n\nSee you at the event!'),
        from_email=settings.DEFAULT_FROM_EMAIL, to=[purchase.buyer_email])
    email.attach(f'ticket_{purchase.paystack_reference}.{ext}', image_bytes, f'image/{ext}')
    email.send(fail_silently=True)
    return HttpResponse(status=200)


def verify_ticket_view(request):
    ticket_found = error_message = None
    if request.method == 'POST':
        purchase = TicketPurchase.objects.filter(paystack_reference=(request.POST.get('reference') or '').strip().upper()).first()
        if purchase and purchase.status == 'Success':
            ticket_found = purchase
        else:
            error_message = 'No paid ticket found for that reference.'
    return render(request, 'voting/verify_ticket.html', {'ticket_found': ticket_found, 'error_message': error_message})


@event_permission_required('ticket.manage')
def event_scanner(request, event):
    return render(request, 'voting/scanner.html', {'event': event})


@event_permission_required('ticket.manage')
@require_POST
def process_scan(request, event):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Invalid request body.'}, status=400)
    reference = None
    for line in str(data.get('text', ''))[:1000].split('\n'):
        if line.startswith('REF:'):
            reference = line.replace('REF:', '').strip()
            break
    if not reference:
        return JsonResponse({'status': 'error', 'message': 'Invalid QR Code (No reference found).'}, status=400)
    with transaction.atomic():
        purchase = TicketPurchase.objects.select_for_update().select_related('ticket').filter(
            event=event, paystack_reference=reference).first()
        if not purchase:
            return JsonResponse({'status': 'error', 'message': 'Ticket not found for this event.'}, status=404)
        if purchase.status != 'Success':
            return JsonResponse({'status': 'error', 'message': 'Payment pending or failed.'}, status=400)
        if purchase.is_checked_in:
            return JsonResponse({'status': 'error', 'message': f'ALREADY USED! Checked in at '
                                 f'{timezone.localtime(purchase.checked_in_at).strftime("%I:%M %p")} by {purchase.buyer_name}.'},
                                status=409)
        purchase.is_checked_in = True
        purchase.checked_in_at = timezone.now()
        purchase.save(update_fields=['is_checked_in', 'checked_in_at'])
    audit.record('TICKET_CHECKED_IN', request=request, event=event, target=purchase, summary=f'Ticket {reference} checked in')
    return JsonResponse({'status': 'success', 'message': f'Welcome, {purchase.buyer_name}! {purchase.quantity} {purchase.ticket.name} ticket(s).'})


@login_required
def edit_ticket(request, ticket_id):
    ticket = get_object_or_404(Ticket.objects.select_related('event'), id=ticket_id)
    event = ticket.event
    check_perm(request.user, 'ticket.manage', event)
    if request.method == 'POST':
        price, price_error = parse_non_negative_decimal(request.POST.get('price'), 'Price')
        old_price, old_price_error = parse_non_negative_decimal(request.POST.get('old_price'), 'Old price', required=False)
        quantity, quantity_error = parse_non_negative_int(request.POST.get('quantity_available'), 'Quantity available')
        error = price_error or old_price_error or quantity_error
        if error:
            messages.error(request, error)
            return render(request, 'voting/edit_ticket.html', {'ticket': ticket, 'event': event})
        ticket.name = (request.POST.get('name') or ticket.name).strip()[:100]
        ticket.price, ticket.old_price, ticket.quantity_available = price, old_price, quantity
        ticket.is_active = request.POST.get('is_active') == 'on'
        if 'image' in request.FILES:
            ticket.image = request.FILES['image']
        ticket.save()
        audit.record('TICKET_TYPE_UPDATED', request=request, event=event, target=ticket, summary=f'Ticket "{ticket.name}" updated')
        messages.success(request, f'Ticket "{ticket.name}" updated successfully.')
        return redirect('event_detail', event_id=event.id)
    return render(request, 'voting/edit_ticket.html', {'ticket': ticket, 'event': event})


@login_required
@require_POST
def delete_ticket(request, ticket_id):
    ticket = get_object_or_404(Ticket.objects.select_related('event'), id=ticket_id)
    event = ticket.event
    check_perm(request.user, 'ticket.manage', event)
    if ticket.purchases.filter(status='Success').exists():
        ticket.is_active = False
        ticket.save(update_fields=['is_active'])
        messages.success(request, f'Ticket "{ticket.name}" has sales, so it was hidden instead of deleted.')
    else:
        audit.record('TICKET_TYPE_DELETED', request=request, event=event, target=ticket, summary=f'Ticket "{ticket.name}" deleted')
        ticket.delete()
        messages.success(request, 'Ticket deleted.')
    return redirect('event_detail', event_id=event.id)


@ratelimit('retrieve-ticket', 10, 60, key='ip')
def retrieve_ticket_view(request):
    error_message = None
    if request.method == 'POST':
        phone_or_ref = (request.POST.get('phone_or_ref') or '').strip()[:40]
        if phone_or_ref.upper().startswith('TK-'):
            purchase = TicketPurchase.objects.filter(paystack_reference=phone_or_ref.upper()).first()
        else:
            clean_phone = phone_or_ref.replace('+', '').replace(' ', '')
            purchase = TicketPurchase.objects.filter(buyer_email=f'{clean_phone}@ussd.vote', status='Success') \
                .order_by('-purchased_at').first()
        if purchase and purchase.status == 'Success':
            return redirect(f"{reverse('ticket_success')}?reference={purchase.paystack_reference}")
        error_message = 'No paid ticket found. Please check your reference code or phone number.'
    return render(request, 'voting/retrieve_ticket.html', {'error_message': error_message})
