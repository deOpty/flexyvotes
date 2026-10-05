"""Paid voting: Pay -> verify -> entitlement -> votes credited.

Rule: the browser's "payment successful" is never proof of payment. Votes are
credited only by :func:`apply_gateway_result` with data that came from
Paystack itself (server-to-server verification, or a signature-verified
webhook), after amount and currency checks, under a row lock, exactly once.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core import audit, crypto, metrics
from core.rbac import check_perm, has_perm
from core.utils import client_ip, device_hash, user_agent
from voting.models import Candidate, Event, TicketPurchase, VoteTransaction

from . import paystack
from .models import (DiscountCode, Payment, PaymentEvent, ReconciliationItem, ReconciliationRun, Refund,
                     VotePackage, WebhookEvent)

logger = logging.getLogger(__name__)
CENT = Decimal('0.01')
P = Payment.Status


class PaymentError(Exception):
    def __init__(self, message, code='invalid'):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
@dataclass
class Quote:
    votes: int = 0
    bonus_votes: int = 0
    unit_price: Decimal = Decimal('0')
    gross: Decimal = Decimal('0')
    discount: Decimal = Decimal('0')
    amount: Decimal = Decimal('0')
    currency: str = 'GHS'
    package: VotePackage = None
    discount_code: DiscountCode = None
    errors: list = field(default_factory=list)

    @property
    def total_votes(self):
        return self.votes + self.bonus_votes

    def as_dict(self):
        return {'votes': self.votes, 'bonus_votes': self.bonus_votes, 'total_votes': self.total_votes,
                'unit_price': str(self.unit_price), 'gross': str(self.gross), 'discount': str(self.discount),
                'amount': str(self.amount), 'currency': self.currency,
                'package': self.package.pk if self.package else None,
                'discount_code': self.discount_code.code if self.discount_code else None, 'errors': self.errors}


def unit_price(event, candidate):
    if candidate.category_id and candidate.category.vote_price is not None:
        return candidate.category.vote_price
    return event.vote_price


def _payer_scope(event, email_index='', phone_index=''):
    query = Q()
    if email_index:
        query |= Q(payer_email_index=email_index)
    if phone_index:
        query |= Q(payer_phone_index=phone_index)
    if not query:
        return Payment.objects.none()
    return Payment.objects.filter(query, event=event)


def _find_discount(event, code):
    if not code:
        return None
    discount = DiscountCode.objects.filter(code=code.strip().upper()).first()
    if discount is None:
        return None
    if discount.event_id and discount.event_id != event.pk:
        return None
    if discount.organization_id and discount.organization_id != event.organization_id:
        return None
    return discount


def quote(event, candidate, *, votes=None, package=None, discount_code='', payer_email='', payer_phone=''):
    q = Quote(currency=event.currency)
    if not event.is_paid:
        q.errors.append('This election does not use paid voting.')
        return q
    if candidate.event_id != event.pk:
        q.errors.append('Invalid contestant.')
        return q
    if candidate.status != Candidate.Status.ACTIVE:
        q.errors.append(f'{candidate.name} is no longer accepting votes.')
    if not event.accepting_votes():
        q.errors.append('Voting is not open right now.')
    price = unit_price(event, candidate)
    q.unit_price = price
    if package is not None:
        if package.event_id != event.pk or not package.is_available():
            q.errors.append('That vote package is not available.')
            return q
        q.package = package
        q.votes, q.bonus_votes = package.votes, package.bonus_votes
        q.gross = package.price
    else:
        try:
            votes = int(votes or 0)
        except (TypeError, ValueError):
            votes = 0
        if votes < max(1, event.min_votes_per_transaction):
            q.errors.append(f'Buy at least {max(1, event.min_votes_per_transaction)} vote(s).')
        if event.max_votes_per_transaction and votes > event.max_votes_per_transaction:
            q.errors.append(f'At most {event.max_votes_per_transaction} votes per payment.')
        if price <= 0:
            q.errors.append('Vote pricing has not been configured.')
        q.votes = max(votes, 0)
        q.gross = (price * q.votes).quantize(CENT, rounding=ROUND_HALF_UP)

    email_index = crypto.blind_index(payer_email, 'email') if payer_email else ''
    from core.utils import normalize_phone

    phone_index = crypto.blind_index(normalize_phone(payer_phone), 'phone') if payer_phone else ''
    discount = _find_discount(event, discount_code)
    if discount_code and (discount is None or not discount.is_available()):
        q.errors.append('That discount code is not valid.')
        discount = None
    if discount is not None:
        if discount.min_amount and q.gross < discount.min_amount:
            q.errors.append(f'That code needs a minimum purchase of {discount.min_amount}.')
        elif discount.per_payer_limit and email_index and Payment.objects.filter(
                discount=discount, payer_email_index=email_index, status=P.SUCCESS).count() >= discount.per_payer_limit:
            q.errors.append('You have already used that discount code.')
        else:
            q.discount_code = discount
            if discount.kind == DiscountCode.Kind.PERCENT:
                q.discount = (q.gross * min(discount.value, Decimal('100')) / 100).quantize(CENT, rounding=ROUND_HALF_UP)
            elif discount.kind == DiscountCode.Kind.FIXED:
                q.discount = min(discount.value, q.gross)
            else:
                q.bonus_votes += int(discount.value)
    q.amount = max(q.gross - q.discount, Decimal('0')).quantize(CENT)
    if q.amount <= 0 and not q.errors:
        q.errors.append('The amount to pay must be greater than zero.')

    # -- per-voter limits (email / phone; card checked after payment) --------
    if email_index or phone_index:
        prior = _payer_scope(event, email_index, phone_index).filter(status=P.SUCCESS, votes_credited=True)
        prior_votes = prior.aggregate(v=Sum(F('votes') + F('bonus_votes')))['v'] or 0
        if event.max_votes_per_voter and prior_votes + q.total_votes > event.max_votes_per_voter:
            q.errors.append(f'Each voter may buy at most {event.max_votes_per_voter} votes in this election '
                            f'({prior_votes} already purchased).')
        if candidate.max_votes_per_voter:
            on_candidate = prior.filter(candidate=candidate).aggregate(v=Sum(F('votes') + F('bonus_votes')))['v'] or 0
            if on_candidate + q.total_votes > candidate.max_votes_per_voter:
                q.errors.append(f'Each voter may give {candidate.name} at most {candidate.max_votes_per_voter} votes.')
        if candidate.category_id and candidate.category.max_votes_per_voter:
            in_category = prior.filter(candidate__category=candidate.category) \
                .aggregate(v=Sum(F('votes') + F('bonus_votes')))['v'] or 0
            if in_category + q.total_votes > candidate.category.max_votes_per_voter:
                q.errors.append(f'Each voter may cast at most {candidate.category.max_votes_per_voter} votes in '
                                f'"{candidate.category.name}".')
        if event.max_spend_per_voter:
            spent = prior.aggregate(a=Sum('amount'))['a'] or Decimal('0')
            if spent + q.amount > event.max_spend_per_voter:
                q.errors.append(f'Spending limit reached: at most {event.max_spend_per_voter} {event.currency} per voter.')
        if q.package and q.package.max_per_payer:
            bought = prior.filter(package=q.package).count()
            if bought >= q.package.max_per_payer:
                q.errors.append(f'The "{q.package.name}" package is limited to {q.package.max_per_payer} per voter.')
    return q


# ---------------------------------------------------------------------------
# Payment state machine
# ---------------------------------------------------------------------------
ALLOWED_TRANSITIONS = {
    P.INITIALIZED: {P.PENDING, P.SUCCESS, P.FAILED, P.ABANDONED},
    P.PENDING: {P.SUCCESS, P.FAILED, P.ABANDONED},
    P.ABANDONED: {P.SUCCESS, P.FAILED},  # late success after the customer came back
    P.FAILED: {P.SUCCESS},  # gateway retried successfully
    P.SUCCESS: {P.REFUNDED, P.PARTIALLY_REFUNDED, P.REVERSED, P.DISPUTED},
    P.PARTIALLY_REFUNDED: {P.REFUNDED, P.DISPUTED, P.REVERSED},
    P.DISPUTED: {P.SUCCESS, P.REVERSED, P.REFUNDED},
    P.REVERSED: set(),
    P.REFUNDED: set(),
}


def _record(payment, source, message='', data=None, actor=None, from_status=None, to_status=None):
    PaymentEvent.objects.create(payment=payment, from_status=from_status or '', to_status=to_status or '',
                                source=source, message=message[:500], data=_sanitize(data or {}), actor=actor)


def _sanitize(data):
    """Keep payment history useful without storing card/customer PII."""
    if not isinstance(data, dict):
        return {}
    keep = ('status', 'gateway_response', 'channel', 'currency', 'amount', 'fees', 'paid_at', 'id', 'message',
            'event', 'reason', 'refund_id', 'display_text', 'kind')
    clean = {k: data[k] for k in keep if k in data}
    if isinstance(data.get('authorization'), dict):
        clean['card'] = {k: data['authorization'].get(k) for k in ('last4', 'brand', 'bank', 'country_code')}
    return json.loads(crypto.canonical_json(clean))


def transition(payment, to_status, source, message='', data=None, actor=None):
    if payment.status == to_status:
        return False
    if to_status not in ALLOWED_TRANSITIONS.get(payment.status, set()):
        _record(payment, source, f'Ignored transition {payment.status} -> {to_status}: {message}', data, actor)
        return False
    previous = payment.status
    payment.status = to_status
    payment.save(update_fields=['status', 'updated_at'])
    _record(payment, source, message, data, actor, previous, to_status)
    metrics.PAYMENTS.labels(status=to_status).inc()
    return True


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------
def initiate_vote_payment(request, event, candidate, *, votes=None, package=None, discount_code='', email='',
                          phone='', name='', idempotency_key=None, channel_hint='', mobile_money=None):
    """Create (or, for a repeated idempotency key, return) a payment and its
    Paystack checkout URL. Never credits anything."""
    from fraud.engine import assess
    from fraud.models import FraudEvent

    if not paystack.configured():
        raise PaymentError('Paid voting is not available right now (payment provider not configured).', 'unavailable')
    if idempotency_key:
        existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            if existing.event_id != event.pk or existing.candidate_id != candidate.pk:
                raise PaymentError('This request was already used for a different purchase.', 'idempotency_conflict')
            return existing
    q = quote(event, candidate, votes=votes, package=package, discount_code=discount_code,
              payer_email=email, payer_phone=phone)
    if q.errors:
        raise PaymentError(q.errors[0])
    if not email and not phone:
        raise PaymentError('Enter an email address or phone number for your receipt.')
    risk = assess(FraudEvent.Kind.PAYMENT, request=request, event=event, candidate=candidate, email=email,
                  phone=phone, amount=q.amount, votes=q.total_votes)
    if risk.challenged:
        from core.captcha import provider_config

        if provider_config()[0] and not getattr(request, '_fv_captcha_passed', False):
            raise PaymentError('Additional verification is required. Complete the challenge and try again.', 'challenge')
    payment = Payment(
        reference=Payment.new_reference(), idempotency_key=idempotency_key or None, purpose=Payment.Purpose.VOTE,
        event=event, candidate=candidate, package=q.package, discount=q.discount_code, votes=q.votes,
        bonus_votes=q.bonus_votes, unit_price=q.unit_price, gross_amount=q.gross, discount_amount=q.discount,
        amount=q.amount, currency=event.currency, payer_name=name[:150], ip_address=client_ip(request),
        device_hash=device_hash(request), user_agent=user_agent(request), risk_score=risk.score,
        risk_decision=risk.decision, channel=channel_hint,
    )
    payment.set_payer(email, phone)
    if risk.held:
        payment.held = True
        payment.hold_reason = 'Risk score %d at checkout' % risk.score
    try:
        with transaction.atomic():
            payment.save()
            _record(payment, 'INIT', f'Payment created (risk {risk.score} {risk.decision})',
                    {'amount': str(q.amount), 'currency': event.currency}, to_status=P.INITIALIZED)
            if risk.event_id:
                FraudEvent.objects.filter(pk=risk.event_id).update(payment=payment)
    except IntegrityError:
        # Concurrent duplicate with the same idempotency key.
        existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing
        raise
    gateway_email = email or f'{payment.reference.lower()}@payments.flexyvotes.invalid'
    metadata = {'purpose': 'vote', 'payment_id': str(payment.pk), 'event_id': event.pk,
                'candidate_id': candidate.pk, 'votes': payment.total_votes}
    try:
        if mobile_money:
            # USSD: charge the subscriber's wallet directly (no checkout page).
            data = paystack.charge_mobile_money(
                email=gateway_email, amount_minor=payment.amount_minor, currency=payment.currency,
                phone=mobile_money['phone'], provider=mobile_money['provider'], reference=payment.reference,
                metadata=metadata)
        else:
            data = paystack.initialize(
                email=gateway_email, amount_minor=payment.amount_minor, currency=payment.currency,
                reference=payment.reference, callback_url=f'{settings.SITE_URL}/payments/callback/',
                metadata=metadata, channels=event.payment_channels or None,
            )
    except paystack.PaystackError as exc:
        with transaction.atomic():
            transition(payment, P.FAILED, 'INIT', f'Gateway initialization failed: {exc}')
        raise PaymentError(str(exc), 'gateway') from exc
    with transaction.atomic():
        Payment.objects.filter(pk=payment.pk).update(authorization_url=data.get('authorization_url', ''),
                                                     access_code=data.get('access_code', ''))
        payment.refresh_from_db()
        transition(payment, P.PENDING, 'INIT', 'Mobile money prompt sent' if mobile_money else
                   'Customer redirected to Paystack checkout', data if mobile_money else None)
    return payment


# ---------------------------------------------------------------------------
# Applying gateway truth
# ---------------------------------------------------------------------------
def _gateway_paid_at(value):
    if not value:
        return None
    parsed = parse_datetime(value) if isinstance(value, str) else value
    return parsed


def apply_gateway_result(payment_or_reference, data, source, actor=None):
    """Idempotent: safe to call any number of times, from any source, in any order."""
    from fraud.engine import assess
    from fraud.models import FraudEvent

    reference = payment_or_reference.reference if isinstance(payment_or_reference, Payment) else payment_or_reference
    with transaction.atomic():
        payment = Payment.objects.select_for_update().filter(reference=reference).first()
        if payment is None:
            return None
        gateway_status = (data.get('status') or '').lower()
        payment.gateway_status = gateway_status[:30]
        if gateway_status == 'success':
            authorization = data.get('authorization') or {}
            payment.channel = (data.get('channel') or payment.channel or '')[:30]
            payment.card_signature = (authorization.get('signature') or '')[:100]
            payment.card_last4 = (authorization.get('last4') or '')[:4]
            payment.card_brand = (authorization.get('brand') or '')[:30]
            payment.card_bank = (authorization.get('bank') or '')[:80]
            payment.card_country = (authorization.get('country_code') or '')[:4]
            payment.gateway_id = str(data.get('id') or '')[:40]
            payment.gateway_ip = data.get('ip_address') or None
            if data.get('fees') is not None:
                payment.fees = (Decimal(str(data['fees'])) / 100).quantize(CENT)
            payment.gateway_paid_at = _gateway_paid_at(data.get('paid_at') or data.get('paidAt'))
            payment.verified_at = timezone.now()
            payment.save()
            amount_ok = int(data.get('amount') or 0) == payment.amount_minor
            currency_ok = (data.get('currency') or payment.currency).upper() == payment.currency.upper()
            changed = transition(payment, P.SUCCESS, source, 'Payment confirmed by Paystack', data, actor)
            if not changed and payment.status != P.SUCCESS:
                return payment
            if not (amount_ok and currency_ok):
                payment.held = True
                payment.hold_reason = (f'Amount/currency mismatch: expected {payment.amount_minor} {payment.currency}, '
                                       f'gateway reported {data.get("amount")} {data.get("currency")}')
                payment.save(update_fields=['held', 'hold_reason'])
                FraudEvent.objects.create(kind=FraudEvent.Kind.PAYMENT, decision=FraudEvent.Decision.HOLD, score=90,
                                          payment=payment, event=payment.event, candidate=payment.candidate,
                                          signals=[{'code': 'amount_mismatch', 'weight': 90, 'detail': payment.hold_reason}])
            elif payment.purpose == Payment.Purpose.VOTE and not payment.held and not payment.votes_credited:
                risk = assess(FraudEvent.Kind.PAYMENT, event=payment.event, candidate=payment.candidate,
                              email=payment.payer_email, phone=payment.payer_phone, device=payment.device_hash,
                              ip=payment.ip_address, card_signature=payment.card_signature,
                              card_country=payment.card_country, votes=payment.total_votes, payment=payment,
                              count_velocity=False)
                payment.risk_score = max(payment.risk_score or 0, risk.score)
                payment.risk_decision = risk.decision if risk.score >= (payment.risk_score or 0) else payment.risk_decision
                if risk.held:
                    payment.held = True
                    payment.hold_reason = f'Risk score {risk.score} after payment'
                payment.save(update_fields=['risk_score', 'risk_decision', 'held', 'hold_reason'])
            if payment.purpose == Payment.Purpose.VOTE and not payment.held:
                credit_votes(payment, actor=actor)
            elif payment.purpose == Payment.Purpose.INVOICE and payment.invoice_id and amount_ok and currency_ok:
                from billing.service import mark_invoice_paid

                mark_invoice_paid(payment.invoice, payment)
        elif gateway_status in ('failed',):
            transition(payment, P.FAILED, source, data.get('gateway_response') or 'Payment failed', data, actor)
        elif gateway_status == 'abandoned':
            transition(payment, P.ABANDONED, source, 'Customer abandoned checkout', data, actor)
        elif gateway_status == 'reversed':
            if transition(payment, P.REVERSED, source, 'Payment reversed by the gateway', data, actor):
                reverse_votes(payment, 'Payment reversed', actor)
        else:
            payment.save(update_fields=['gateway_status'])
            transition(payment, P.PENDING, source, f'Gateway status: {gateway_status or "unknown"}', data, actor)
    return payment


def credit_votes(payment, actor=None):
    """Turn a verified payment into votes - at most once (OneToOne + flag)."""
    if payment.votes_credited or payment.status != P.SUCCESS or payment.held:
        return None
    try:
        with transaction.atomic():
            ledger = VoteTransaction.objects.create(
                candidate=payment.candidate, payment=payment, voter_email=f'paid-vote@{payment.event_id}.flexyvotes.internal',
                amount=payment.amount, paystack_reference=payment.reference, status=VoteTransaction.Status.SUCCESS,
                vote_type=VoteTransaction.VoteType.MAIN, number_of_votes=payment.total_votes,
            )
    except IntegrityError:
        return None  # another worker credited it first
    Payment.objects.filter(pk=payment.pk).update(votes_credited=True, credited_at=timezone.now())
    payment.votes_credited = True
    if payment.discount_id:
        DiscountCode.objects.filter(pk=payment.discount_id).update(redemptions_count=F('redemptions_count') + 1)
    _record(payment, 'CREDIT', f'{payment.total_votes} vote(s) credited to {payment.candidate.name}', actor=actor)
    metrics.VOTES.labels(mode='paid').inc(payment.total_votes)
    metrics.VOTE_SUBMISSIONS.labels(mode='paid', outcome='success').inc()
    cache.delete(f'fv:live:{payment.event_id}')
    transaction.on_commit(lambda: _send_payment_confirmation(payment.pk))
    return ledger


def _send_payment_confirmation(payment_id):
    from notifications.models import Notification
    from notifications.service import notify

    payment = Payment.objects.select_related('event', 'candidate').filter(pk=payment_id).first()
    if payment is None:
        return None
    context = {'amount': f'{payment.currency} {payment.amount}', 'reference': payment.reference,
               'votes': payment.total_votes, 'candidate': payment.candidate.name}
    if payment.payer_email:
        return notify('payment_confirmation', channel=Notification.Channel.EMAIL, recipient=payment.payer_email,
                      event=payment.event, context=context, dedupe_key=f'paid:{payment.pk}')
    if payment.payer_phone:
        return notify('payment_confirmation', channel=Notification.Channel.SMS, recipient=payment.payer_phone,
                      event=payment.event, context=context, dedupe_key=f'paid:{payment.pk}')
    return None


def reverse_votes(payment, reason, actor=None):
    updated = VoteTransaction.objects.filter(payment=payment, status=VoteTransaction.Status.SUCCESS) \
        .update(status=VoteTransaction.Status.REVERSED)
    if updated:
        _record(payment, 'REVERSE', f'Credited votes reversed: {reason}', actor=actor)
        cache.delete(f'fv:live:{payment.event_id}')
    return updated


def release_held_payment(payment, analyst, notes=''):
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        payment.held = False
        payment.hold_reason = ''
        payment.save(update_fields=['held', 'hold_reason'])
        _record(payment, 'REVIEW', f'Hold released by analyst: {notes}', actor=analyst)
        credit_votes(payment, actor=analyst)
    audit.record('PAYMENT_HOLD_RELEASED', actor=analyst, event=payment.event, target=payment, reason=notes)
    return payment


def reject_held_payment(payment, analyst, notes=''):
    _record(payment, 'REVIEW', f'Held payment rejected (votes not credited): {notes}', actor=analyst)
    audit.record('PAYMENT_HOLD_REJECTED', actor=analyst, event=payment.event, target=payment, reason=notes)
    if payment.status == P.SUCCESS and has_perm(analyst, 'refund.create', payment.event):
        request_refund(payment, payment.amount, f'Rejected by fraud review: {notes}', analyst)
    return payment


def verify_and_apply(reference, source='VERIFY', actor=None):
    try:
        data = paystack.verify(reference)
    except paystack.PaystackError as exc:
        logger.warning('Verification failed for %s: %s', reference, exc)
        return None
    if Payment.objects.filter(reference=reference).exists():
        return apply_gateway_result(reference, data, source, actor)
    if TicketPurchase.objects.filter(paystack_reference=reference).exists():
        return apply_ticket_result(reference, data)
    return None


def apply_ticket_result(reference, data):
    """Tickets keep their legacy model but follow the same verification rules."""
    with transaction.atomic():
        purchase = TicketPurchase.objects.select_for_update().select_related('ticket').filter(paystack_reference=reference).first()
        if purchase is None or purchase.status != 'Pending':
            return purchase
        status = (data.get('status') or '').lower()
        if status == 'success':
            expected = int((purchase.expected_amount * 100).quantize(Decimal('1')))
            if int(data.get('amount') or 0) != expected:
                logger.error('Ticket %s amount mismatch: expected %s got %s', reference, expected, data.get('amount'))
                return purchase
            purchase.status = 'Success'
        elif status in ('failed', 'abandoned', 'reversed'):
            purchase.status = 'Failed'
        else:
            return purchase
        purchase.save(update_fields=['status'])
    return purchase


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
def handle_webhook(body: bytes, signature: str):
    """Returns (http_status, outcome). Signature first; then replay-safe."""
    if not paystack.verify_signature(body, signature or ''):
        metrics.WEBHOOKS.labels(provider='paystack', outcome='bad_signature').inc()
        audit.record('WEBHOOK_SIGNATURE_INVALID', result='DENIED', summary='Paystack webhook with invalid signature')
        return 401, 'invalid signature'
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        metrics.WEBHOOKS.labels(provider='paystack', outcome='malformed').inc()
        return 400, 'malformed'
    event_type = str(payload.get('event', ''))[:60]
    data = payload.get('data') or {}
    reference = str(data.get('reference') or (data.get('transaction') or {}).get('reference') or '')[:100]
    digest = crypto.sha256_hex(body)
    record, created = WebhookEvent.objects.get_or_create(payload_hash=digest, defaults={
        'event_type': event_type, 'reference': reference, 'payload': _sanitize(data) | {'event': event_type}})
    if not created:
        WebhookEvent.objects.filter(pk=record.pk).update(attempts=F('attempts') + 1)
        if record.status in (WebhookEvent.Status.PROCESSED, WebhookEvent.Status.IGNORED):
            metrics.WEBHOOKS.labels(provider='paystack', outcome='duplicate').inc()
            return 200, 'duplicate'
    try:
        outcome = _dispatch_webhook(event_type, data, reference)
        record.status = WebhookEvent.Status.PROCESSED if outcome != 'ignored' else WebhookEvent.Status.IGNORED
        record.error = ''
    except Exception as exc:  # noqa: BLE001 - recorded, retried by Paystack and by reconciliation
        logger.exception('Webhook processing failed for %s', reference)
        record.status = WebhookEvent.Status.FAILED
        record.error = str(exc)[:1000]
        outcome = 'failed'
    record.processed_at = timezone.now()
    record.save(update_fields=['status', 'error', 'processed_at'])
    metrics.WEBHOOKS.labels(provider='paystack', outcome=outcome).inc()
    return (500 if outcome == 'failed' else 200), outcome


def _dispatch_webhook(event_type, data, reference):
    if event_type == 'charge.success':
        if Payment.objects.filter(reference=reference).exists():
            apply_gateway_result(reference, data, 'WEBHOOK')
            return 'processed'
        if TicketPurchase.objects.filter(paystack_reference=reference).exists():
            apply_ticket_result(reference, data)
            return 'processed'
        return 'ignored'
    if event_type in ('charge.failed',):
        if Payment.objects.filter(reference=reference).exists():
            apply_gateway_result(reference, {**data, 'status': 'failed'}, 'WEBHOOK')
            return 'processed'
        return 'ignored'
    if event_type.startswith('refund.'):
        return _webhook_refund(event_type, data)
    if event_type.startswith('charge.dispute.'):
        return _webhook_dispute(event_type, data, reference)
    return 'ignored'


def _webhook_refund(event_type, data):
    transaction_ref = str((data.get('transaction') or {}).get('reference') or data.get('transaction_reference') or '')
    refund = Refund.objects.filter(payment__reference=transaction_ref).exclude(
        status__in=[Refund.Status.PROCESSED, Refund.Status.REJECTED]).order_by('-requested_at').first()
    if refund is None:
        return 'ignored'
    if event_type in ('refund.processed',):
        _complete_refund(refund)
    elif event_type == 'refund.failed':
        Refund.objects.filter(pk=refund.pk).update(status=Refund.Status.FAILED, error=str(data.get('status', ''))[:500])
        _record(refund.payment, 'WEBHOOK', 'Refund failed at gateway', data)
    else:
        _record(refund.payment, 'WEBHOOK', f'Refund update: {event_type}', data)
    return 'processed'


def _webhook_dispute(event_type, data, reference):
    from fraud.engine import record_chargeback

    reference = reference or str((data.get('transaction') or {}).get('reference') or '')
    payment = Payment.objects.filter(reference=reference).first()
    if payment is None:
        return 'ignored'
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if event_type == 'charge.dispute.create':
            if transition(payment, P.DISPUTED, 'WEBHOOK', 'Chargeback opened by cardholder', data):
                reverse_votes(payment, 'Chargeback opened')
                record_chargeback(payment, 'Chargeback opened')
        elif event_type == 'charge.dispute.resolve':
            resolution = str(data.get('resolution') or data.get('status') or '').lower()
            if 'merchant' in resolution or resolution in ('declined', 'won'):
                transition(payment, P.SUCCESS, 'WEBHOOK', 'Chargeback resolved in merchant favour; votes stay reversed pending review', data)
            else:
                transition(payment, P.REVERSED, 'WEBHOOK', 'Chargeback lost', data)
    audit.record('PAYMENT_CHARGEBACK', event=payment.event, target=payment, summary=f'{event_type} for {reference}')
    return 'processed'


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------
def request_refund(payment, amount, reason, actor, request=None, reverse=True):
    payment = Payment.objects.select_related('event').get(pk=payment.pk)
    check_perm(actor, 'refund.create', payment.event)
    amount = Decimal(str(amount)).quantize(CENT)
    refundable = payment.amount - payment.refunded_amount
    if payment.status not in (P.SUCCESS, P.PARTIALLY_REFUNDED, P.DISPUTED):
        raise PaymentError('Only successful payments can be refunded.')
    if amount <= 0 or amount > refundable:
        raise PaymentError(f'Refund amount must be between 0.01 and {refundable}.')
    refund = Refund.objects.create(payment=payment, amount=amount, reason=reason, requested_by=actor,
                                   reverse_votes=reverse)
    _record(payment, 'REFUND', f'Refund of {amount} requested: {reason}', actor=actor)
    audit.record('REFUND_REQUESTED', request=request, actor=actor, event=payment.event, target=refund,
                 summary=f'Refund {amount} {payment.currency} requested for {payment.reference}', reason=reason)
    threshold = Decimal(settings.REFUND_DUAL_APPROVAL_THRESHOLD)
    if amount > threshold:
        from elections.integrity import request_approval
        from elections.models import ApprovalRequest

        request_approval(payment.event, ApprovalRequest.Action.REFUND, {'refund_id': str(refund.pk)}, reason, actor,
                         request)
        refund.refresh_from_db()
    elif has_perm(actor, 'refund.approve', payment.event):
        refund = approve_refund(refund.pk, actor, via_approval=True)
    return refund


def approve_refund(refund_id, approver, via_approval=False):
    refund = Refund.objects.select_related('payment', 'payment__event').get(pk=refund_id)
    if not via_approval:
        check_perm(approver, 'refund.approve', refund.payment.event)
        if approver.pk == refund.requested_by_id:
            raise PaymentError('Separation of duties: a different person must approve this refund.')
    if refund.status != Refund.Status.REQUESTED:
        return refund
    refund.status = Refund.Status.APPROVED
    refund.approved_by = approver
    refund.save(update_fields=['status', 'approved_by'])
    try:
        data = paystack.refund(refund.payment.reference, int((refund.amount * 100).quantize(Decimal('1'))))
    except paystack.PaystackError as exc:
        refund.status = Refund.Status.FAILED
        refund.error = str(exc)[:1000]
        refund.save(update_fields=['status', 'error'])
        _record(refund.payment, 'REFUND', f'Gateway refused refund: {exc}', actor=approver)
        return refund
    refund.status = Refund.Status.PROCESSING
    refund.gateway_refund_id = str(data.get('id', ''))[:40]
    refund.save(update_fields=['status', 'gateway_refund_id'])
    _record(refund.payment, 'REFUND', 'Refund submitted to Paystack', {'refund_id': refund.gateway_refund_id},
            actor=approver)
    audit.record('REFUND_APPROVED', actor=approver, event=refund.payment.event, target=refund,
                 summary=f'Refund {refund.amount} approved and submitted')
    if paystack.fake_mode():
        _complete_refund(refund)
    return refund


def _complete_refund(refund):
    with transaction.atomic():
        refund = Refund.objects.select_for_update().select_related('payment').get(pk=refund.pk)
        if refund.status == Refund.Status.PROCESSED:
            return refund
        payment = Payment.objects.select_for_update().get(pk=refund.payment_id)
        refund.status = Refund.Status.PROCESSED
        refund.processed_at = timezone.now()
        refund.save(update_fields=['status', 'processed_at'])
        payment.refunded_amount += refund.amount
        payment.save(update_fields=['refunded_amount'])
        full = payment.refunded_amount >= payment.amount
        transition(payment, P.REFUNDED if full else P.PARTIALLY_REFUNDED, 'REFUND',
                   f'Refund of {refund.amount} processed')
        if refund.reverse_votes:
            reverse_votes(payment, 'Refunded')
    audit.record('REFUND_PROCESSED', event=payment.event, target=refund, summary=f'Refund {refund.amount} processed')
    return refund


# ---------------------------------------------------------------------------
# Reconciliation & housekeeping
# ---------------------------------------------------------------------------
def reconcile(window_start=None, window_end=None, actor=None):
    now = timezone.now()
    window_end = window_end or now
    window_start = window_start or (window_end - timedelta(hours=24))
    run = ReconciliationRun.objects.create(window_start=window_start, window_end=window_end, triggered_by=actor)
    checked = discrepancies = resolved = 0

    def item(kind, payment, reference, resolution, note='', gateway_status='', gateway_amount=None):
        nonlocal discrepancies, resolved
        discrepancies += 1
        if resolution == ReconciliationItem.Resolution.AUTO_RESOLVED:
            resolved += 1
        metrics.RECONCILIATION_DISCREPANCIES.labels(kind=kind).inc()
        return ReconciliationItem.objects.create(
            run=run, payment=payment, reference=reference, kind=kind, resolution=resolution, note=note[:1000],
            local_status=payment.status if payment else '', gateway_status=gateway_status,
            local_amount=payment.amount if payment else None, gateway_amount=gateway_amount)

    try:
        seen = set()
        if not paystack.fake_mode():
            page = 1
            while True:
                rows, meta = paystack.list_transactions(window_start, window_end, page=page)
                for row in rows:
                    checked += 1
                    reference = str(row.get('reference', ''))
                    seen.add(reference)
                    gateway_status = str(row.get('status', '')).lower()
                    gateway_amount = Decimal(str(row.get('amount') or 0)) / 100
                    payment = Payment.objects.filter(reference=reference).first()
                    if payment is None:
                        ticket = TicketPurchase.objects.filter(paystack_reference=reference).first()
                        if ticket is not None:
                            if gateway_status == 'success' and ticket.status != 'Success':
                                apply_ticket_result(reference, row)
                            continue
                        if gateway_status == 'success':
                            item(ReconciliationItem.Kind.MISSING_LOCALLY, None, reference,
                                 ReconciliationItem.Resolution.NEEDS_REVIEW, 'Successful at Paystack, unknown here',
                                 gateway_status, gateway_amount)
                        continue
                    if gateway_status == 'success' and int(row.get('amount') or 0) != payment.amount_minor:
                        item(ReconciliationItem.Kind.AMOUNT_MISMATCH, payment, reference,
                             ReconciliationItem.Resolution.NEEDS_REVIEW, 'Amount differs', gateway_status, gateway_amount)
                    local_success = payment.status in (P.SUCCESS, P.PARTIALLY_REFUNDED, P.REFUNDED, P.DISPUTED)
                    if (gateway_status == 'success') != local_success or \
                            (gateway_status in ('failed', 'abandoned') and payment.status not in (P.FAILED, P.ABANDONED)):
                        before = payment.status
                        apply_gateway_result(payment, row, 'RECONCILE', actor)
                        payment.refresh_from_db()
                        item(ReconciliationItem.Kind.STATUS_MISMATCH, payment, reference,
                             ReconciliationItem.Resolution.AUTO_RESOLVED if payment.status != before else
                             ReconciliationItem.Resolution.NEEDS_REVIEW,
                             f'{before} -> {payment.status}', gateway_status, gateway_amount)
                if page >= int(meta.get('pageCount') or 1):
                    break
                page += 1
        stale = Payment.objects.filter(created_at__gte=window_start, created_at__lte=window_end - timedelta(minutes=5),
                                       status__in=[P.INITIALIZED, P.PENDING]).exclude(reference__in=seen)
        for payment in stale[:500]:
            checked += 1
            before = payment.status
            verify_and_apply(payment.reference, 'RECONCILE', actor)
            payment.refresh_from_db()
            if payment.status != before:
                item(ReconciliationItem.Kind.STATUS_MISMATCH, payment, payment.reference,
                     ReconciliationItem.Resolution.AUTO_RESOLVED, f'{before} -> {payment.status} (missed webhook)',
                     payment.gateway_status)
        uncredited = Payment.objects.filter(created_at__gte=window_start, status=P.SUCCESS, votes_credited=False,
                                            held=False, purpose=Payment.Purpose.VOTE)
        for payment in uncredited:
            checked += 1
            if credit_votes(payment, actor):
                item(ReconciliationItem.Kind.NOT_CREDITED, payment, payment.reference,
                     ReconciliationItem.Resolution.AUTO_RESOLVED, 'Votes credited during reconciliation')
        if not paystack.fake_mode():
            missing = Payment.objects.filter(created_at__gte=window_start, created_at__lte=window_end,
                                             status=P.SUCCESS).exclude(reference__in=seen)
            for payment in missing[:200]:
                checked += 1
                try:
                    data = paystack.verify(payment.reference)
                except paystack.PaystackError:
                    data = {}
                if (data.get('status') or '').lower() != 'success':
                    item(ReconciliationItem.Kind.NOT_AT_GATEWAY, payment, payment.reference,
                         ReconciliationItem.Resolution.NEEDS_REVIEW, 'Marked successful here; Paystack disagrees',
                         str(data.get('status', 'not found')))
        run.status = ReconciliationRun.Status.COMPLETED
    except Exception as exc:  # noqa: BLE001
        logger.exception('Reconciliation failed')
        run.status = ReconciliationRun.Status.FAILED
        run.error = str(exc)[:2000]
    run.checked_count, run.discrepancy_count, run.resolved_count = checked, discrepancies, resolved
    run.finished_at = timezone.now()
    run.save()
    audit.record('PAYMENTS_RECONCILED', actor=actor, target=run,
                 summary=f'Reconciliation: {checked} checked, {discrepancies} discrepancies, {resolved} auto-resolved',
                 result='SUCCESS' if run.status == ReconciliationRun.Status.COMPLETED else 'FAILURE')
    return run


def expire_abandoned(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=settings.PAYMENT_ABANDON_AFTER_MINUTES)
    count = 0
    for payment in Payment.objects.filter(status__in=[P.INITIALIZED, P.PENDING], created_at__lt=cutoff)[:500]:
        result = verify_and_apply(payment.reference, 'EXPIRY')
        payment.refresh_from_db()
        if payment.status in (P.INITIALIZED, P.PENDING):
            with transaction.atomic():
                locked = Payment.objects.select_for_update().get(pk=payment.pk)
                transition(locked, P.ABANDONED, 'EXPIRY',
                           f'No completion after {settings.PAYMENT_ABANDON_AFTER_MINUTES} minutes')
            count += 1
        elif result is None:
            continue
    for purchase in TicketPurchase.objects.filter(status='Pending', purchased_at__lt=cutoff)[:500]:
        verify_and_apply(purchase.paystack_reference, 'EXPIRY')
        TicketPurchase.objects.filter(pk=purchase.pk, status='Pending').update(status='Failed')
    return count


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def revenue_report(events, start=None, end=None):
    payments = Payment.objects.filter(event__in=events, purpose=Payment.Purpose.VOTE)
    if start:
        payments = payments.filter(created_at__gte=start)
    if end:
        payments = payments.filter(created_at__lt=end)
    successful = payments.filter(status__in=[P.SUCCESS, P.PARTIALLY_REFUNDED, P.REFUNDED, P.DISPUTED])
    totals = successful.aggregate(gross=Sum('amount'), fees=Sum('fees'), refunds=Sum('refunded_amount'),
                                  votes=Sum(F('votes') + F('bonus_votes')), count=Count('pk'))
    gross = totals['gross'] or Decimal('0')
    refunds = totals['refunds'] or Decimal('0')
    by_event = []
    for event in events:
        row = successful.filter(event=event).aggregate(gross=Sum('amount'), refunds=Sum('refunded_amount'),
                                                       votes=Sum(F('votes') + F('bonus_votes')), count=Count('pk'))
        net = (row['gross'] or Decimal('0')) - (row['refunds'] or Decimal('0'))
        fee = (net * event.platform_fee_percentage / 100).quantize(CENT)
        by_event.append({'event': event, 'gross': row['gross'] or Decimal('0'), 'refunds': row['refunds'] or Decimal('0'),
                         'net': net, 'platform_fee': fee, 'organizer_payout': net - fee,
                         'votes': row['votes'] or 0, 'payments': row['count']})
    by_day = list(successful.annotate(day=TruncDate('created_at')).values('day')
                  .annotate(amount=Sum('amount'), payments=Count('pk')).order_by('day'))
    by_channel = list(successful.values('channel').annotate(amount=Sum('amount'), payments=Count('pk')).order_by('-amount'))
    status_counts = dict(payments.values_list('status').annotate(n=Count('pk')))
    attempted = sum(status_counts.values())
    success_count = sum(n for s, n in status_counts.items() if s in (P.SUCCESS, P.PARTIALLY_REFUNDED, P.REFUNDED, P.DISPUTED))
    return {
        'gross': gross, 'fees': totals['fees'] or Decimal('0'), 'refunds': refunds, 'net': gross - refunds,
        'votes': totals['votes'] or 0, 'payments': totals['count'] or 0, 'by_event': by_event,
        'by_day': by_day, 'by_channel': by_channel, 'status_counts': status_counts,
        'success_rate': round(100.0 * success_count / attempted, 1) if attempted else 0.0,
        'held': payments.filter(held=True, status=P.SUCCESS).count(),
    }


# ---------------------------------------------------------------------------
# SaaS invoices
# ---------------------------------------------------------------------------
def initiate_invoice_payment(request, invoice, email):
    """Pay an organization invoice through Paystack (same verification rules)."""
    if not paystack.configured():
        raise PaymentError('Online payment is not available right now.', 'unavailable')
    if invoice.status != 'OPEN' or invoice.total <= 0:
        raise PaymentError('This invoice is not awaiting payment.')
    existing = Payment.objects.filter(invoice=invoice, status__in=[P.INITIALIZED, P.PENDING]).first()
    if existing is not None and existing.authorization_url:
        return existing
    payment = Payment(reference=Payment.new_reference(), purpose=Payment.Purpose.INVOICE, invoice=invoice,
                      gross_amount=invoice.total, amount=invoice.total, currency=invoice.currency,
                      ip_address=client_ip(request), device_hash=device_hash(request), user_agent=user_agent(request))
    payment.set_payer(email, '')
    with transaction.atomic():
        payment.save()
        _record(payment, 'INIT', f'Invoice {invoice.number} payment created', to_status=P.INITIALIZED)
    try:
        data = paystack.initialize(email=email, amount_minor=payment.amount_minor, currency=payment.currency,
                                   reference=payment.reference, callback_url=f'{settings.SITE_URL}/payments/callback/',
                                   metadata={'purpose': 'invoice', 'invoice': invoice.number})
    except paystack.PaystackError as exc:
        with transaction.atomic():
            transition(payment, P.FAILED, 'INIT', str(exc))
        raise PaymentError(str(exc), 'gateway') from exc
    with transaction.atomic():
        Payment.objects.filter(pk=payment.pk).update(authorization_url=data['authorization_url'])
        payment.refresh_from_db()
        transition(payment, P.PENDING, 'INIT', 'Redirected to Paystack for invoice payment')
    return payment
