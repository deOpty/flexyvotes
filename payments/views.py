import csv
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core import audit, captcha, crypto
from core.ratelimit import ratelimit
from core.rbac import check_perm, event_permission_required, events_for_user, has_perm
from voting.models import Candidate, Event
from voting.views import parse_non_negative_decimal, parse_non_negative_int, sanitize_csv_value

from . import paystack, service
from .models import DiscountCode, Payment, ReconciliationItem, ReconciliationRun, Refund, VotePackage

IDEMPOTENCY_SESSION = 'fv_pay_keys'


# ---------------------------------------------------------------------------
# Paying voter
# ---------------------------------------------------------------------------
def _paid_event(event_id, candidate_id):
    event = get_object_or_404(Event, pk=event_id)
    if not event.is_paid or not event.is_public:
        raise Http404
    candidate = get_object_or_404(Candidate.objects.select_related('category'), pk=candidate_id, event=event)
    return event, candidate


@ratelimit('pay', 20, 60, key='ip')
def pay(request, event_id, candidate_id):
    event, candidate = _paid_event(event_id, candidate_id)
    packages = [p for p in event.vote_packages.all() if p.is_available()]
    context = {'event': event, 'candidate': candidate, 'packages': packages,
               'unit_price': service.unit_price(event, candidate), 'accepting': event.accepting_votes(),
               'prefill_votes': request.GET.get('votes') or '', 'gateway_ready': paystack.configured()}
    if request.method != 'POST':
        context['idempotency_key'] = uuid.uuid4().hex
        return render(request, 'payments/pay.html', context)
    context['idempotency_key'] = (request.POST.get('idempotency_key') or uuid.uuid4().hex)[:64]
    context['form'] = request.POST
    human, _ = captcha.verify_human(request)
    if not human:
        messages.error(request, 'Please complete the verification and try again.')
        return render(request, 'payments/pay.html', context, status=400)
    name, _ = captcha.provider_config()
    request._fv_captcha_passed = bool(name)
    package = event.vote_packages.filter(pk=request.POST.get('package') or 0).first()
    try:
        payment = service.initiate_vote_payment(
            request, event, candidate, votes=request.POST.get('votes'), package=package,
            discount_code=(request.POST.get('discount_code') or '').strip(), email=(request.POST.get('email') or '').strip(),
            phone=(request.POST.get('phone') or '').strip(), name=(request.POST.get('name') or '').strip(),
            idempotency_key=f"web:{context['idempotency_key']}")
    except service.PaymentError as exc:
        messages.error(request, exc.message)
        return render(request, 'payments/pay.html', context, status=400)
    if payment.status in (Payment.Status.PENDING, Payment.Status.INITIALIZED) and payment.authorization_url:
        return redirect(payment.authorization_url)
    return redirect('payments:receipt', reference=payment.reference)


def quote_view(request, event_id, candidate_id):
    event, candidate = _paid_event(event_id, candidate_id)
    package = event.vote_packages.filter(pk=request.GET.get('package') or 0).first()
    q = service.quote(event, candidate, votes=request.GET.get('votes'), package=package,
                      discount_code=(request.GET.get('discount_code') or '')[:40])
    return JsonResponse(q.as_dict())


def callback(request):
    reference = (request.GET.get('reference') or request.GET.get('trxref') or '').strip()[:40]
    if not reference or not Payment.objects.filter(reference=reference).exists():
        messages.error(request, 'Unknown payment reference.')
        return redirect('home')
    # The redirect itself proves nothing; ask Paystack.
    service.verify_and_apply(reference, 'CALLBACK')
    return redirect('payments:receipt', reference=reference)


def receipt(request, reference):
    payment = get_object_or_404(Payment.objects.select_related('event', 'candidate'), reference=reference)
    if payment.status in (Payment.Status.INITIALIZED, Payment.Status.PENDING) and request.GET.get('check'):
        service.verify_and_apply(reference, 'CALLBACK')
        payment.refresh_from_db()
    return render(request, 'payments/receipt.html', {'payment': payment,
                                                     'pending': payment.status in (Payment.Status.INITIALIZED, Payment.Status.PENDING)})


@csrf_exempt
@require_POST
def webhook(request):
    status, outcome = service.handle_webhook(request.body, request.headers.get('x-paystack-signature', ''))
    return HttpResponse(outcome, status=status, content_type='text/plain')


def simulator(request, reference):
    """Development-only stand-in for the Paystack checkout page."""
    if not paystack.fake_mode():
        raise Http404
    payment = Payment.objects.filter(reference=reference).first()
    store = paystack.fake_store(reference)
    if store is None:
        raise Http404
    if request.method == 'POST':
        outcome = request.POST.get('outcome')
        status = {'pay': 'success', 'fail': 'failed', 'abandon': 'abandoned'}.get(outcome, 'abandoned')
        store.update({'status': status, 'paid_at': timezone.now().isoformat() if status == 'success' else None,
                      'card_signature': request.POST.get('card') or 'SIG_FAKE_CARD', 'country': request.POST.get('country') or 'GH'})
        paystack.fake_set(reference, store)
        if status in ('success', 'failed'):
            data = paystack.verify(reference)
            body = json.dumps({'event': 'charge.success' if status == 'success' else 'charge.failed', 'data': data}).encode()
            service.handle_webhook(body, paystack.signature_for(body))
        if payment is None:
            return redirect(f"{reverse('ticket_success')}?reference={reference}")
        return redirect(f"{reverse('payments:callback')}?reference={reference}")
    return render(request, 'payments/simulator.html', {'reference': reference, 'store': store, 'payment': payment,
                                                       'amount': Decimal(store['amount']) / 100})


# ---------------------------------------------------------------------------
# Finance console
# ---------------------------------------------------------------------------
@login_required
def payments_list(request):
    events = events_for_user(request.user, 'payment.view')
    payments = Payment.objects.filter(Q(event__in=events) | Q(event__isnull=True) if has_perm(request.user, 'platform.admin')
                                      else Q(event__in=events)).select_related('event', 'candidate')
    event_id = request.GET.get('event')
    if event_id:
        payments = payments.filter(event_id=event_id)
    status = request.GET.get('status')
    if status in Payment.Status.values:
        payments = payments.filter(status=status)
    if request.GET.get('held'):
        payments = payments.filter(held=True)
    query = (request.GET.get('q') or '').strip()
    if query:
        if '@' in query:
            payments = payments.filter(payer_email_index=crypto.blind_index(query, 'email'))
        else:
            payments = payments.filter(reference__icontains=query.upper())
    return render(request, 'payments/console/list.html', {
        'page': Paginator(payments, 50).get_page(request.GET.get('page')), 'events': events.order_by('-created_at')[:200],
        'statuses': Payment.Status.choices, 'filters': request.GET})


@login_required
def payment_detail(request, reference):
    payment = get_object_or_404(Payment.objects.select_related('event', 'candidate', 'package', 'discount'), reference=reference)
    check_perm(request.user, 'payment.view', payment.event)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'verify':
                check_perm(request.user, 'payment.reconcile', payment.event)
                service.verify_and_apply(payment.reference, 'MANUAL', actor=request.user)
                messages.success(request, 'Status refreshed from Paystack.')
            elif action == 'refund':
                amount, error = parse_non_negative_decimal(request.POST.get('amount'), 'Amount')
                if error:
                    raise service.PaymentError(error)
                refund = service.request_refund(payment, amount, (request.POST.get('reason') or '').strip() or 'Refund',
                                                request.user, request, reverse=request.POST.get('reverse_votes') == 'on')
                messages.success(request, f'Refund {refund.get_status_display().lower()}.')
            elif action == 'approve_refund':
                refund = service.approve_refund(request.POST.get('refund'), request.user)
                messages.success(request, f'Refund {refund.get_status_display().lower()}.')
            elif action in ('release', 'reject'):
                check_perm(request.user, 'fraud.review', payment.event)
                if action == 'release':
                    service.release_held_payment(payment, request.user, request.POST.get('notes', ''))
                    messages.success(request, 'Hold released; votes credited.')
                else:
                    service.reject_held_payment(payment, request.user, request.POST.get('notes', ''))
                    messages.success(request, 'Held payment rejected.')
        except PermissionDenied:
            messages.error(request, 'You do not have permission to do that.')
        except service.PaymentError as exc:
            messages.error(request, exc.message)
        return redirect('payments:console_detail', reference=payment.reference)
    audit.record('PAYMENT_VIEWED', request=request, event=payment.event, target=payment, summary=f'Payment {reference} viewed')
    return render(request, 'payments/console/detail.html', {
        'payment': payment, 'history': payment.history.select_related('actor'), 'refunds': payment.refunds.all(),
        'fraud_events': payment.fraud_events.all(), 'ledger': getattr(payment, 'vote_transaction', None)
        if hasattr(payment, 'vote_transaction') else None})


@login_required
def reconciliation(request):
    """Tenant-scoped. Platform admins see and run everything. A finance
    officer sees and resolves discrepancies on their own organization's
    payments; an auditor (audit.view + payment.view) sees them read-only.
    Runs cover the platform's single Paystack account, so only platform
    admins start them or see their totals."""
    user = request.user
    platform = has_perm(user, 'platform.admin')
    resolvable_ids = set(events_for_user(user, 'payment.reconcile').values_list('pk', flat=True))
    auditable_ids = set(events_for_user(user, 'audit.view').values_list('pk', flat=True))         & set(events_for_user(user, 'payment.view').values_list('pk', flat=True))
    if not (platform or resolvable_ids or auditable_ids):
        raise PermissionDenied
    items = ReconciliationItem.objects.filter(resolution=ReconciliationItem.Resolution.NEEDS_REVIEW)         .select_related('payment')
    if not platform:
        items = items.filter(payment__event_id__in=resolvable_ids | auditable_ids)
    if request.method == 'POST':
        if request.POST.get('action') == 'run':
            if not platform:
                raise PermissionDenied
            hours = min(max(int(request.POST.get('hours') or 24), 1), 24 * 30)
            run = service.reconcile(timezone.now() - timedelta(hours=hours), timezone.now(), actor=user)
            messages.success(request, f'Reconciliation finished: {run.checked_count} checked, '
                                      f'{run.discrepancy_count} discrepancies, {run.resolved_count} auto-resolved.')
        elif request.POST.get('action') == 'resolve':
            item = get_object_or_404(items, pk=request.POST.get('item'))
            if not (platform or (item.payment and item.payment.event_id in resolvable_ids)):
                raise PermissionDenied
            item.resolution = ReconciliationItem.Resolution.RESOLVED
            item.note = (request.POST.get('note') or '')[:1000]
            item.resolved_by = user
            item.resolved_at = timezone.now()
            item.save()
            audit.record('RECONCILIATION_ITEM_RESOLVED', request=request, target=item, reason=item.note)
        return redirect('payments:reconciliation')
    runs = ReconciliationRun.objects.order_by('-started_at', '-pk')
    return render(request, 'payments/console/reconciliation.html', {
        'platform': platform,
        'runs': runs[:30] if platform else [],
        'last_run': runs.first(),
        'open_items': items[:200],
        'resolvable_ids': resolvable_ids,
    })


def _date(value, default):
    try:
        return timezone.make_aware(datetime.strptime(value, '%Y-%m-%d'))
    except (TypeError, ValueError):
        return default


@login_required
def revenue(request):
    events = events_for_user(request.user, 'payment.view').filter(voting_mode=Event.VotingMode.PAY_TO_VOTE)
    end = _date(request.GET.get('to'), timezone.now()) + (timedelta(days=1) if request.GET.get('to') else timedelta())
    start = _date(request.GET.get('from'), timezone.now() - timedelta(days=30))
    if request.GET.get('event'):
        events = events.filter(pk=request.GET['event'])
    report = service.revenue_report(list(events), start, end)
    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="revenue.csv"'
        writer = csv.writer(response)
        writer.writerow(['Election', 'Payments', 'Votes', 'Gross', 'Refunds', 'Net', 'Platform fee', 'Organizer payout'])
        for row in report['by_event']:
            writer.writerow([sanitize_csv_value(row['event'].title), row['payments'], row['votes'], row['gross'],
                             row['refunds'], row['net'], row['platform_fee'], row['organizer_payout']])
        audit.record('REVENUE_EXPORTED', request=request, summary='Revenue report exported')
        return response
    return render(request, 'payments/console/revenue.html', {'report': report, 'start': start, 'end': end,
                                                             'events': events})


@event_permission_required('pricing.manage')
def pricing(request, event):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'package':
            votes, votes_error = parse_non_negative_int(request.POST.get('votes'), 'Votes')
            bonus, _ = parse_non_negative_int(request.POST.get('bonus_votes') or '0', 'Bonus votes')
            price, price_error = parse_non_negative_decimal(request.POST.get('price'), 'Price')
            name = (request.POST.get('name') or '').strip()[:80]
            error = votes_error or price_error or (None if name and votes and price else 'Name, votes and price are required.')
            if error:
                messages.error(request, error)
            else:
                package = VotePackage.objects.create(
                    event=event, name=name, votes=votes, bonus_votes=bonus or 0, price=price,
                    badge=(request.POST.get('badge') or '')[:40], is_promotional=request.POST.get('is_promotional') == 'on',
                    starts_at=_date(request.POST.get('starts_at'), None), ends_at=_date(request.POST.get('ends_at'), None),
                    max_per_payer=parse_non_negative_int(request.POST.get('max_per_payer'), 'Limit', required=False)[0])
                audit.record('PRICING_PACKAGE_CREATED', request=request, event=event, target=package,
                             summary=f'Package {name}: {votes}+{bonus} votes for {price}')
                messages.success(request, 'Package added.')
        elif action == 'toggle_package':
            package = get_object_or_404(VotePackage, pk=request.POST.get('package'), event=event)
            package.is_active = not package.is_active
            package.save(update_fields=['is_active'])
            audit.record('PRICING_PACKAGE_TOGGLED', request=request, event=event, target=package,
                         changes={'is_active': {'old': not package.is_active, 'new': package.is_active}})
        elif action == 'discount':
            value, error = parse_non_negative_decimal(request.POST.get('value'), 'Value')
            code = (request.POST.get('code') or '').strip().upper()[:40]
            kind = request.POST.get('kind') if request.POST.get('kind') in DiscountCode.Kind.values else DiscountCode.Kind.PERCENT
            if error or not code:
                messages.error(request, error or 'Code is required.')
            elif DiscountCode.objects.filter(code=code).exists():
                messages.error(request, 'That code already exists.')
            elif kind == DiscountCode.Kind.PERCENT and value > 100:
                messages.error(request, 'Percent discounts cannot exceed 100.')
            else:
                discount = DiscountCode.objects.create(
                    event=event, code=code, kind=kind, value=value, created_by=request.user,
                    max_redemptions=parse_non_negative_int(request.POST.get('max_redemptions'), 'Max', required=False)[0],
                    per_payer_limit=parse_non_negative_int(request.POST.get('per_payer_limit'), 'Limit', required=False)[0],
                    starts_at=_date(request.POST.get('starts_at'), None), ends_at=_date(request.POST.get('ends_at'), None))
                audit.record('PRICING_DISCOUNT_CREATED', request=request, event=event, target=discount,
                             summary=f'Discount {code} ({kind} {value})')
                messages.success(request, 'Discount code created.')
        elif action == 'toggle_discount':
            discount = get_object_or_404(DiscountCode, pk=request.POST.get('discount'), event=event)
            discount.is_active = not discount.is_active
            discount.save(update_fields=['is_active'])
        return redirect('payments:pricing', event_id=event.pk)
    return render(request, 'payments/console/pricing.html', {
        'event': event, 'packages': event.vote_packages.all(), 'discounts': event.discount_codes.all(),
        'discount_kinds': DiscountCode.Kind.choices})


@login_required
def refunds(request):
    events = events_for_user(request.user, 'refund.approve')
    pending = Refund.objects.filter(payment__event__in=events).select_related('payment', 'requested_by', 'approved_by')
    return render(request, 'payments/console/refunds.html', {'refunds': pending[:200]})
