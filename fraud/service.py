"""Analyst actions on fraud alerts and held payments."""
from django.db import transaction
from django.utils import timezone

from core import audit, crypto
from core.rbac import check_perm

from .models import BlocklistEntry, FraudEvent


class FraudReviewError(Exception):
    pass


def review(fraud_event, analyst, outcome, notes='', request=None, block=False):
    """outcome: 'dismiss' (legitimate - releases a held payment) or
    'confirm' (fraud - keeps votes uncredited and optionally blocklists)."""
    check_perm(analyst, 'fraud.review', fraud_event.event)
    if fraud_event.status != FraudEvent.Status.OPEN:
        raise FraudReviewError('This alert has already been reviewed.')
    with transaction.atomic():
        fraud_event.status = FraudEvent.Status.DISMISSED if outcome == 'dismiss' else FraudEvent.Status.CONFIRMED
        fraud_event.reviewed_by = analyst
        fraud_event.reviewed_at = timezone.now()
        fraud_event.notes = notes
        fraud_event.save()
        payment = fraud_event.payment
        if payment is not None and payment.held:
            from payments.service import reject_held_payment, release_held_payment

            if outcome == 'dismiss':
                release_held_payment(payment, analyst, notes)
            else:
                reject_held_payment(payment, analyst, notes)
        if outcome == 'confirm' and block:
            # Platform admins block platform-wide; tenant analysts only for
            # their own organization's events.
            from core.rbac import is_platform_admin

            scope = None if is_platform_admin(analyst) else getattr(fraud_event.event, 'organization', None)
            reason = f'Fraud alert #{fraud_event.pk}'
            if fraud_event.device_hash:
                add_block(BlocklistEntry.Kind.DEVICE, fraud_event.device_hash, analyst, reason, organization=scope)
            if payment is not None and payment.card_signature:
                add_block(BlocklistEntry.Kind.CARD, payment.card_signature, analyst, reason, organization=scope)
            if payment is not None and payment.payer_email_index:
                add_block(BlocklistEntry.Kind.EMAIL, payment.payer_email_index, analyst, reason, raw=False,
                          organization=scope)
        audit.record('FRAUD_ALERT_REVIEWED', request=request, actor=analyst, event=fraud_event.event,
                     target=fraud_event, summary=f'Fraud alert {fraud_event.pk} {fraud_event.status.lower()}',
                     reason=notes, metadata={'score': fraud_event.score, 'blocked': block})
    return fraud_event


def add_block(kind, value, actor, reason='', raw=True, expires_at=None, organization=None):
    """``organization=None`` makes the entry platform-wide."""
    value = (value or '').strip()
    if raw and kind == BlocklistEntry.Kind.EMAIL:
        value = crypto.blind_index(value, 'email')
    elif raw and kind == BlocklistEntry.Kind.PHONE:
        from core.utils import normalize_phone

        value = crypto.blind_index(normalize_phone(value), 'phone')
    elif kind == BlocklistEntry.Kind.EMAIL_DOMAIN:
        value = value.lower().lstrip('@')
    if not value:
        raise FraudReviewError('A value is required.')
    if kind in (BlocklistEntry.Kind.CIDR, BlocklistEntry.Kind.ANONYMIZER, BlocklistEntry.Kind.IP):
        import ipaddress

        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise FraudReviewError('Invalid IP address or range.') from exc
    entry, _ = BlocklistEntry.objects.update_or_create(kind=kind, value=value, organization=organization, defaults={
        'reason': reason, 'is_active': True, 'created_by': actor, 'expires_at': expires_at})
    audit.record('BLOCKLIST_ADDED', actor=actor, organization=organization, target=entry,
                 summary=f'{entry.get_kind_display()} blocklisted' + ('' if organization else ' platform-wide'),
                 reason=reason)
    return entry
